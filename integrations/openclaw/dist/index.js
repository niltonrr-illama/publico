import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  closeSync,
  constants as fsConstants,
  copyFileSync,
  createReadStream,
  createWriteStream,
  existsSync,
  fchmodSync,
  fstatSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  unlinkSync,
  writeSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { Transform } from "node:stream";
import { pipeline } from "node:stream/promises";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const PLUGIN_ID = "espelho-zap-portable";
const PASSIVE_REASON = "espelho-zap-passive";
const ALLOWED_SCOPES = new Set(["area_shared", "partnership_restricted", "owner_private"]);
const MAX_MEDIA = 8;
const MAX_MEDIA_BYTES = 128 * 1024 * 1024;
const MAX_TOTAL_MEDIA_BYTES = 256 * 1024 * 1024;
const OUTBOUND_LEDGER_COMPACT_BYTES = 8 * 1024 * 1024;
const OUTBOUND_LEDGER_HARD_BYTES = 128 * 1024 * 1024;
const OUTBOUND_CAPTURE_PENDING_MAX = 256;
const OUTBOUND_CAPTURE_RETENTION_MS = 2 * 60 * 1000;
const MEDIA_PLACEHOLDER = /^<media:(?:image|audio|voice|video|document|file)>$/i;
const FORUM_DATA_PLANE_REASON = "espelho-zap-forum-data-plane";
let ingestTail = Promise.resolve();
let outboundTail = Promise.resolve();

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function asNonEmpty(value) {
  if (typeof value === "string" && value.length > 0) return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

function safeErrorCode(error) {
  const value = error instanceof Error ? error.message : "";
  return /^[A-Za-z0-9_]+$/.test(value) ? value : "adapter_error";
}

function asTimestamp(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    const milliseconds = Math.abs(value) >= 100_000_000_000 ? value : value * 1000;
    const rendered = new Date(milliseconds);
    if (!Number.isNaN(rendered.getTime())) return rendered.toISOString();
    return null;
  }
  if (typeof value !== "string" || value.length === 0) return null;
  // A timezone is mandatory; never reinterpret a host-local timestamp.
  if (!/(?:Z|[+-][0-9]{2}:[0-9]{2})$/i.test(value)) return null;
  const rendered = new Date(value);
  return Number.isNaN(rendered.getTime()) ? null : rendered.toISOString();
}

function platformOf(event, ctx) {
  const candidates = [
    event?.platform,
    event?.channel,
    event?.channelId,
    event?.metadata?.platform,
    event?.metadata?.channelId,
    ctx?.platform,
    ctx?.channel,
    ctx?.channelId,
    ctx?.messageProvider,
  ];
  const found = candidates.map(asNonEmpty).find((value) => value !== null);
  return found?.toLowerCase() ?? null;
}

function kindOf(value, filePath) {
  const kind = String(value ?? "").split(";", 1)[0].trim().toLowerCase();
  if (["image", "photo"].includes(kind)) return "image";
  if (["voice", "ptt"].includes(kind)) return "voice";
  if (kind === "video") return "video";
  if (["document", "file"].includes(kind)) return "document";
  const suffix = path.extname(filePath).toLowerCase();
  if (kind.startsWith("image/")) return "image";
  if (kind.startsWith("video/")) return "video";
  if (
    [".ogg", ".oga", ".opus"].includes(suffix)
    || kind === "audio/ogg"
    || kind === "audio/opus"
  ) return "voice";
  if (kind === "audio" || kind.startsWith("audio/")) return "audio";
  if ([".jpg", ".jpeg", ".png", ".webp", ".gif"].includes(suffix)) return "image";
  if ([".mp3", ".m4a", ".wav", ".aac"].includes(suffix)) return "audio";
  if ([".mp4", ".mov", ".webm"].includes(suffix)) return "video";
  return "document";
}

function rootsFromEnvironment() {
  return String(process.env.ESPELHO_ZAP_MEDIA_ROOTS ?? "")
    .split(path.delimiter)
    .filter(Boolean)
    .map((item) => realpathSync(item));
}

function contained(candidate, roots) {
  return roots.some((root) => candidate === root || candidate.startsWith(`${root}${path.sep}`));
}

function boundedFileName(value, fallback) {
  let candidate = path.basename(asNonEmpty(value) ?? fallback).replaceAll("\0", "");
  if (!candidate || candidate === "." || candidate === "..") candidate = fallback;
  const extension = path.extname(candidate).slice(0, 16);
  let stem = candidate.slice(0, candidate.length - extension.length);
  while (stem && Buffer.byteLength(`${stem}${extension}`, "utf8") > 180) {
    stem = stem.slice(0, -1);
  }
  return stem ? `${stem}${extension}` : `media${extension}`;
}

function mediaStagingPending(event) {
  return event?.metadata?.mediaStagingPending === true || event?.mediaStagingPending === true;
}

function officialMediaFacts(event) {
  const metadata = event?.metadata && typeof event.metadata === "object" ? event.metadata : {};
  const manyPaths = Array.isArray(metadata.mediaPaths)
    ? metadata.mediaPaths.map(asNonEmpty).filter((value) => value !== null)
    : [];
  const onePath = asNonEmpty(metadata.mediaPath);
  const paths = manyPaths.length > 0 ? manyPaths : (onePath ? [onePath] : []);
  const manyTypes = Array.isArray(metadata.mediaTypes) ? metadata.mediaTypes : [];
  const oneType = asNonEmpty(metadata.mediaType);
  if (paths.length > 0) {
    return paths.map((mediaPath, index) => {
      const mediaType = asNonEmpty(manyTypes[index]) ?? oneType ?? "";
      return {
        path: mediaPath,
        kind: mediaType,
        contentType: mediaType,
        fileName: path.basename(mediaPath),
      };
    });
  }
  // Compatibility only for runtimes that exposed pre-canonical media facts.
  // The v2026.7.1 contract uses metadata.mediaPath(s)/mediaType(s).
  return Array.isArray(event?.media) ? event.media : [];
}

function recordHealth(healthFile, success, errorCode = "") {
  let value = {
    schema_version: 1,
    successes: 0,
    failures: {},
    last_success_at: "",
    last_failure_at: "",
    last_error_code: "",
  };
  try {
    const loaded = JSON.parse(readFileSync(healthFile, "utf8"));
    if (loaded && typeof loaded === "object" && !Array.isArray(loaded)) {
      value = { ...value, ...loaded };
    }
  } catch {
    // Missing or malformed state is replaced by a new private aggregate.
  }
  const now = new Date().toISOString();
  if (success) {
    value.successes = Number(value.successes || 0) + 1;
    value.last_success_at = now;
  } else {
    const safe = /^[A-Za-z0-9_]+$/.test(errorCode) ? errorCode : "adapter_error";
    const failures = value.failures && typeof value.failures === "object"
      ? value.failures
      : {};
    failures[safe] = Number(failures[safe] || 0) + 1;
    value.failures = failures;
    value.last_failure_at = now;
    value.last_error_code = safe;
  }
  mkdirSync(path.dirname(healthFile), { recursive: true, mode: 0o700 });
  const temporary = `${healthFile}.${process.pid}.${Date.now()}.tmp`;
  try {
    writeFileSync(temporary, JSON.stringify(value), { encoding: "utf8", mode: 0o600 });
    renameSync(temporary, healthFile);
    chmodSync(healthFile, 0o600);
  } finally {
    try { unlinkSync(temporary); } catch {}
  }
}

function normalizeMedia(event, profileRef, rawConversation, messageId) {
  const facts = officialMediaFacts(event);
  if (facts.length > MAX_MEDIA) throw new Error("media_count_exceeded");
  if (facts.length === 0) return [];
  const roots = rootsFromEnvironment();
  if (roots.length === 0) throw new Error("media_roots_required");
  return facts.map((fact, index) => {
    const rawPath = asNonEmpty(fact?.path);
    if (!rawPath) throw new Error("media_path_missing");
    const resolved = realpathSync(rawPath);
    if (!contained(resolved, roots)) throw new Error("media_path_outside_root");
    const stat = statSync(resolved);
    if (!stat.isFile()) throw new Error("media_not_file");
    const mimeType = (asNonEmpty(fact?.contentType) ?? "").split(";", 1)[0].trim();
    return {
      media_id: `media:${sha256(`${profileRef}\u001f${rawConversation}\u001f${messageId}\u001f${index}`)}`,
      kind: kindOf(fact?.kind, resolved),
      path: resolved,
      mime_type: mimeType,
      sha256: "",
      size_bytes: stat.size,
      caption: "",
      managed_temp: false,
    };
  });
}

function normalizeEvent(event, ctx, privacyScope, sourceProfileId) {
  if (platformOf(event, ctx) !== "whatsapp") return null;
  // OpenClaw can emit an early fact before staged media is safe to read.
  // Ignore that fact in full and wait for the staged message; otherwise the
  // same message id would conflict later with a richer payload.
  if (mediaStagingPending(event)) return null;
  const explicitDirection = asNonEmpty(event?.direction);
  if (explicitDirection && explicitDirection.toLowerCase() !== "inbound") return null;
  const runtimeProfile = asNonEmpty(ctx?.accountId)
    ?? asNonEmpty(event?.metadata?.accountId)
    ?? sourceProfileId;
  const conversation = asNonEmpty(ctx?.conversationId) ?? asNonEmpty(event?.metadata?.conversationId);
  const thread = asNonEmpty(event?.threadId);
  const session = asNonEmpty(ctx?.sessionKey);
  if (!conversation && !thread && !session) return null;
  // Prefer OpenClaw's stable conversation id so an imported legacy runtime route can
  // be reused verbatim inside the same explicit source profile.  Thread and
  // session are bounded fallbacks, never a destination or contact guess.
  const rawConversation = conversation ?? thread ?? session;
  const rawActor = asNonEmpty(event?.senderId) ?? asNonEmpty(ctx?.senderId);
  const rawMessage = asNonEmpty(event?.messageId) ?? asNonEmpty(ctx?.messageId);
  if (!rawConversation || !rawActor || !rawMessage) return null;
  const profileRef = `profile:${sha256(runtimeProfile)}`;
  const media = normalizeMedia(event, profileRef, rawConversation, rawMessage);
  const rawText = typeof event?.content === "string" ? event.content : "";
  // OpenClaw documents these exact values as technical placeholders for a
  // media-only inbound.  They are not sender captions and must never appear
  // in Telegram.  Any other text, including literal "Description:", remains.
  const text = media.length > 0 && MEDIA_PLACEHOLDER.test(rawText.trim()) ? "" : rawText;
  if (!text && media.length === 0) return null;
  const occurredAt = asTimestamp(event?.occurredAt) ?? asTimestamp(event?.timestamp);
  if (!occurredAt) throw new Error("timestamp_required");
  return {
    schema_version: 2,
    event_id: `event:${sha256(`whatsapp\u001f${profileRef}\u001f${rawConversation}\u001f${rawMessage}`)}`,
    source: "whatsapp",
    source_profile_id: runtimeProfile,
    conversation_id: rawConversation,
    actor_ref: rawActor,
    occurred_at: occurredAt,
    privacy_scope: privacyScope,
    text,
    media,
  };
}

function runIngest(payload) {
  const cli = process.env.ESPELHO_ZAP_CLI;
  const configPath = process.env.ESPELHO_ZAP_CONFIG;
  const maxBytes = Number.parseInt(process.env.ESPELHO_ZAP_MAX_HOOK_BYTES ?? "1048576", 10);
  const timeoutMs = Number.parseInt(process.env.ESPELHO_ZAP_HOOK_TIMEOUT_MS ?? "15000", 10);
  if (!cli || !path.isAbsolute(cli) || !configPath || !path.isAbsolute(configPath)) {
    return Promise.reject(new Error("ingest_command_not_ready"));
  }
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0 || body.length > maxBytes) {
    return Promise.reject(new Error("ingest_payload_too_large"));
  }
  return new Promise((resolve, reject) => {
    const child = spawn(cli, ["--config", configPath, "ingest", "-"], {
      shell: false,
      stdio: ["pipe", "ignore", "ignore"],
      windowsHide: true,
    });
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill("SIGKILL");
      reject(new Error("ingest_timeout"));
    }, timeoutMs);
    child.once("error", () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new Error("ingest_spawn_failed"));
    });
    child.once("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code === 0) resolve();
      else reject(new Error("ingest_rejected"));
    });
    child.stdin.end(body);
  });
}

function queueIngest(payload) {
  const current = ingestTail.then(() => runIngest(payload));
  ingestTail = current.catch(() => undefined);
  return current;
}

function splitIds(value) {
  return new Set(String(value ?? "").split(/[,;\s]+/).filter(Boolean));
}

function isBotOrAssistant(event, ctx) {
  const role = String(event?.role ?? event?.metadata?.role ?? "").toLowerCase();
  if (["assistant", "bot", "system", "tool"].includes(role)) return true;
  return [
    event?.isBot,
    event?.senderIsBot,
    event?.metadata?.isBot,
    event?.metadata?.senderIsBot,
    ctx?.senderIsBot,
  ].some((value) => value === true || String(value).toLowerCase() === "true");
}

function isServiceOrAutomation(event, ctx) {
  const containers = [
    event,
    ctx,
    event?.metadata,
    event?.rawMessage,
    event?.raw_message,
    ctx?.metadata,
    ctx?.rawMessage,
    ctx?.raw_message,
    ctx?.channelContext,
  ].filter((value) => value && typeof value === "object");
  const blockedKinds = new Set([
    "assistant",
    "automation",
    "automatic",
    "bot",
    "service",
    "service_message",
    "system",
    "tool",
  ]);
  for (const container of containers) {
    for (const field of ["eventType", "event_type", "messageType", "message_type", "type", "kind"]) {
      const value = asNonEmpty(container[field]);
      const normalized = value
        ?.trim()
        .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
        .replace(/[-\s]+/g, "_")
        .toLowerCase();
      if (normalized && blockedKinds.has(normalized)) return true;
    }
    for (const field of [
      "automatic",
      "automation",
      "isAutomatic",
      "is_automatic",
      "isAutomation",
      "is_automation",
      "isFromAutomation",
      "is_from_automation",
      "isService",
      "is_service",
      "serviceMessage",
      "service_message",
    ]) {
      const value = container[field];
      if (value === true || ["1", "true", "yes"].includes(String(value).trim().toLowerCase())) return true;
      if (["serviceMessage", "service_message"].includes(field) && value && typeof value === "object") {
        return true;
      }
    }
  }
  return false;
}

function parseTelegramConversation(value) {
  const candidate = asNonEmpty(value)?.trim();
  if (!candidate) return null;
  const raw = candidate.match(/^(-?[0-9]+)$/);
  if (raw) return { chatId: raw[1], threadId: null };
  const canonical = candidate.match(/^telegram:(-?[0-9]+)(?::topic:([0-9]+))?$/i);
  return canonical
    ? { chatId: canonical[1], threadId: canonical[2] ?? null }
    : null;
}

function telegramConversation(event, ctx) {
  const candidates = [
    event?.metadata?.to,
    event?.metadata?.originatingTo,
    ctx?.conversationId,
    event?.metadata?.conversationId,
    ctx?.chatId,
    ctx?.channelId,
    ctx?.channelContext?.chat?.id,
    event?.chatId,
    event?.metadata?.chatId,
    event?.metadata?.chat_id,
  ];
  let fallback = null;
  for (const candidate of candidates) {
    const parsed = parseTelegramConversation(candidate);
    if (!parsed) continue;
    if (parsed.threadId) return parsed;
    fallback ??= parsed;
  }
  return fallback;
}

function telegramChatId(event, ctx) {
  return telegramConversation(event, ctx)?.chatId ?? null;
}

function telegramThreadId(event, ctx) {
  return asNonEmpty(event?.threadId)
    ?? asNonEmpty(event?.metadata?.threadId)
    ?? asNonEmpty(event?.metadata?.messageThreadId)
    ?? asNonEmpty(event?.metadata?.message_thread_id)
    ?? telegramConversation(event, ctx)?.threadId
    ?? asNonEmpty(ctx?.threadId);
}

function telegramSenderId(event, ctx) {
  return asNonEmpty(event?.senderId)
    ?? asNonEmpty(ctx?.senderId)
    ?? asNonEmpty(ctx?.channelContext?.sender?.id)
    ?? asNonEmpty(event?.metadata?.senderId)
    ?? asNonEmpty(event?.metadata?.sender_id);
}

function telegramMessageId(event, ctx) {
  return asNonEmpty(event?.messageId)
    ?? asNonEmpty(ctx?.messageId)
    ?? asNonEmpty(event?.metadata?.messageId)
    ?? asNonEmpty(event?.metadata?.message_id);
}

function telegramOutboundRequestId(event, ctx) {
  const chatId = telegramChatId(event, ctx);
  const threadId = telegramThreadId(event, ctx);
  const messageId = telegramMessageId(event, ctx);
  if (!chatId || !threadId || !messageId) throw new Error("telegram_topic_identity_required");
  return `telegram:${chatId}:${threadId}:${messageId}`;
}

function canonicalWhatsappIdentity(value) {
  const candidate = String(value ?? "").trim();
  if (!candidate || candidate.endsWith("@lid")) return null;
  if (candidate.includes("@")) {
    const boundary = candidate.lastIndexOf("@");
    const local = candidate.slice(0, boundary).replace(/^\+/, "");
    const suffix = candidate.slice(boundary + 1);
    if (suffix === "s.whatsapp.net" && /^[0-9]{6,20}$/.test(local)) {
      return `${local}@s.whatsapp.net`;
    }
    if (suffix === "g.us" && /^[0-9][0-9-]{5,39}$/.test(local)) {
      return `${local}@g.us`;
    }
    return null;
  }
  const local = candidate.replace(/^\+/, "");
  return /^[0-9]{6,20}$/.test(local) ? `${local}@s.whatsapp.net` : null;
}

function lstatOrNull(candidate) {
  try {
    return lstatSync(candidate);
  } catch (error) {
    if (error && typeof error === "object" && error.code === "ENOENT") return null;
    throw error;
  }
}

function fsyncDirectory(directory) {
  if (process.platform === "win32") return;
  const descriptor = openSync(directory, fsConstants.O_RDONLY);
  try { fsyncSync(descriptor); } finally { closeSync(descriptor); }
}

function ensurePrivateDirectory(directory, errorCode) {
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const details = lstatSync(directory);
  if (details.isSymbolicLink() || !details.isDirectory()) throw new Error(errorCode);
  const actual = path.normalize(realpathSync(directory));
  const expected = path.normalize(path.resolve(directory));
  const comparableActual = process.platform === "win32" ? actual.toLowerCase() : actual;
  const comparableExpected = process.platform === "win32" ? expected.toLowerCase() : expected;
  if (comparableActual !== comparableExpected) {
    throw new Error(errorCode);
  }
  if (process.platform === "win32") {
    chmodSync(directory, 0o700);
    return;
  }
  const descriptor = openSync(directory, fsConstants.O_RDONLY);
  try {
    const opened = fstatSync(descriptor);
    const named = lstatSync(directory);
    if (!opened.isDirectory() || named.isSymbolicLink() || !sameFile(opened, named)) {
      throw new Error(errorCode);
    }
    fchmodSync(descriptor, 0o700);
  } finally {
    closeSync(descriptor);
  }
}

function sameFile(first, second) {
  return first.dev === second.dev && first.ino === second.ino;
}

function ensurePrivateRegularFile(fileName, errorCode) {
  const parent = path.dirname(fileName);
  ensurePrivateDirectory(parent, `${errorCode}_parent`);
  let details = lstatOrNull(fileName);
  if (!details) {
    const flags = fsConstants.O_WRONLY
      | fsConstants.O_CREAT
      | fsConstants.O_EXCL
      | (fsConstants.O_NOFOLLOW ?? 0);
    let descriptor;
    try {
      descriptor = openSync(fileName, flags, 0o600);
      fchmodSync(descriptor, 0o600);
      fsyncSync(descriptor);
    } catch (error) {
      if (!(error && typeof error === "object" && error.code === "EEXIST")) throw error;
    } finally {
      if (descriptor !== undefined) closeSync(descriptor);
    }
    if (descriptor !== undefined) fsyncDirectory(parent);
    details = lstatSync(fileName);
  }
  if (details.isSymbolicLink() || !details.isFile()) throw new Error(errorCode);
  const descriptor = openSync(fileName, fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW ?? 0));
  try {
    const opened = fstatSync(descriptor);
    const named = lstatSync(fileName);
    if (!opened.isFile() || named.isSymbolicLink() || !named.isFile() || !sameFile(opened, named)) {
      throw new Error(errorCode);
    }
    fchmodSync(descriptor, 0o600);
  } finally {
    closeSync(descriptor);
  }
}

function openVerifiedRegularFile(fileName, flags, errorCode) {
  ensurePrivateRegularFile(fileName, errorCode);
  const descriptor = openSync(fileName, flags | (fsConstants.O_NOFOLLOW ?? 0));
  try {
    const opened = fstatSync(descriptor);
    const named = lstatSync(fileName);
    if (!opened.isFile() || named.isSymbolicLink() || !named.isFile() || !sameFile(opened, named)) {
      throw new Error(errorCode);
    }
    fchmodSync(descriptor, 0o600);
    return descriptor;
  } catch (error) {
    closeSync(descriptor);
    throw error;
  }
}

function appendOutboundRecord(settings, record) {
  const body = Buffer.from(`${JSON.stringify(record)}\n`, "utf8");
  const descriptor = openVerifiedRegularFile(
    settings.ledgerFile,
    fsConstants.O_WRONLY | fsConstants.O_APPEND,
    "human_outbound_ledger_invalid",
  );
  try {
    writeSync(descriptor, body);
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
  const previous = settings.records.get(record.requestId) ?? {};
  settings.records.set(record.requestId, { ...previous, ...record });
  if (statSync(settings.ledgerFile).size > OUTBOUND_LEDGER_COMPACT_BYTES) {
    replaceOutboundLedger(settings.ledgerFile, settings.records);
  }
}

function replaceOutboundLedger(ledgerFile, records) {
  const temporary = `${ledgerFile}.${process.pid}.${Date.now()}.compact`;
  const body = [...records.values()].map((record) => JSON.stringify(record)).join("\n");
  try {
    ensurePrivateDirectory(path.dirname(ledgerFile), "human_outbound_ledger_invalid_parent");
    ensurePrivateRegularFile(ledgerFile, "human_outbound_ledger_invalid");
    const descriptor = openSync(
      temporary,
      fsConstants.O_WRONLY
        | fsConstants.O_CREAT
        | fsConstants.O_EXCL
        | (fsConstants.O_NOFOLLOW ?? 0),
      0o600,
    );
    try {
      const payload = Buffer.from(body ? `${body}\n` : "", "utf8");
      if (payload.length > 0) writeSync(descriptor, payload);
      fchmodSync(descriptor, 0o600);
      fsyncSync(descriptor);
    } finally {
      closeSync(descriptor);
    }
    renameSync(temporary, ledgerFile);
    const installed = openVerifiedRegularFile(
      ledgerFile,
      fsConstants.O_RDWR,
      "human_outbound_ledger_invalid",
    );
    closeSync(installed);
    fsyncDirectory(path.dirname(ledgerFile));
  } finally {
    try { unlinkSync(temporary); } catch {}
  }
}

function loadOutboundRecords(ledgerFile) {
  ensurePrivateRegularFile(ledgerFile, "human_outbound_ledger_invalid");
  const descriptor = openVerifiedRegularFile(
    ledgerFile,
    fsConstants.O_RDONLY,
    "human_outbound_ledger_invalid",
  );
  let size;
  let body;
  try {
    size = fstatSync(descriptor).size;
    if (size > OUTBOUND_LEDGER_HARD_BYTES) throw new Error("human_outbound_ledger_too_large");
    body = readFileSync(descriptor, "utf8");
  } finally {
    closeSync(descriptor);
  }
  const lines = body.split("\n");
  const records = new Map();
  let truncatedTail = false;
  for (let index = 0; index < lines.length; index += 1) {
    const rawLine = lines[index];
    if (!rawLine) continue;
    let record;
    try {
      record = JSON.parse(rawLine);
    } catch {
      const finalPartial = index === lines.length - 1 && !body.endsWith("\n");
      if (!finalPartial) throw new Error("human_outbound_ledger_invalid");
      truncatedTail = true;
      break;
    }
    const requestId = asNonEmpty(record?.requestId);
    const status = asNonEmpty(record?.status);
    if (!requestId || !["reserved", "dispatching", "sent", "failed", "uncertain"].includes(status)) {
      throw new Error("human_outbound_ledger_invalid");
    }
    records.set(requestId, { ...(records.get(requestId) ?? {}), ...record });
  }
  if (truncatedTail) {
    const backup = `${ledgerFile}.truncated-${Date.now()}.bak`;
    copyFileSync(ledgerFile, backup, fsConstants.COPYFILE_EXCL);
    const backupDetails = lstatSync(backup);
    if (backupDetails.isSymbolicLink() || !backupDetails.isFile()) {
      throw new Error("human_outbound_ledger_invalid");
    }
    chmodSync(backup, 0o600);
    const backupDescriptor = openVerifiedRegularFile(
      backup,
      fsConstants.O_RDWR,
      "human_outbound_ledger_invalid",
    );
    try { fsyncSync(backupDescriptor); } finally { closeSync(backupDescriptor); }
    fsyncDirectory(path.dirname(backup));
  }
  if (truncatedTail || size > OUTBOUND_LEDGER_COMPACT_BYTES) {
    replaceOutboundLedger(ledgerFile, records);
  }
  return records;
}

function humanOutboundSettings() {
  const mode = String(process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_ENABLED ?? "disabled").toLowerCase();
  if (mode === "disabled") return null;
  if (mode !== "enabled") throw new Error("human_outbound_invalid");
  const forumChatId = asNonEmpty(process.env.ESPELHO_ZAP_TELEGRAM_FORUM_CHAT_ID);
  const allowedUsers = splitIds(process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_ALLOWED_USERS);
  const routeMapRaw = asNonEmpty(process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_ROUTE_MAP);
  const ledgerFile = asNonEmpty(process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER);
  const managedRootRaw = asNonEmpty(process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_MANAGED_MEDIA_ROOT);
  const whatsappAccountId = asNonEmpty(process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_WHATSAPP_ACCOUNT_ID);
  if (!forumChatId) throw new Error("human_outbound_forum_required");
  if (allowedUsers.size === 0) throw new Error("human_outbound_allowed_users_required");
  if (!routeMapRaw || !path.isAbsolute(routeMapRaw)) throw new Error("human_outbound_route_map_required");
  if (!ledgerFile || !path.isAbsolute(ledgerFile)) throw new Error("human_outbound_ledger_required");
  if (!managedRootRaw || !path.isAbsolute(managedRootRaw)) throw new Error("human_outbound_managed_media_root_required");
  const routeMap = realpathSync(routeMapRaw);
  if (!statSync(routeMap).isFile()) throw new Error("human_outbound_route_map_required");
  ensurePrivateDirectory(managedRootRaw, "human_outbound_managed_media_root_required");
  const managedRoot = realpathSync(managedRootRaw);
  if (!lstatSync(managedRoot).isDirectory()) throw new Error("human_outbound_managed_media_root_required");
  const settings = {
    forumChatId,
    allowedUsers,
    routeMap,
    ledgerFile,
    managedRoot,
    whatsappAccountId,
    records: loadOutboundRecords(ledgerFile),
  };
  for (const record of settings.records.values()) {
    if (record.status === "dispatching") {
      appendOutboundRecord(settings, {
        requestId: record.requestId,
        status: "uncertain",
        updatedAt: new Date().toISOString(),
      });
    }
  }
  return settings;
}

function reverseRoute(settings, threadId) {
  if (statSync(settings.routeMap).size > 4 * 1024 * 1024) throw new Error("route_map_too_large");
  const value = JSON.parse(readFileSync(settings.routeMap, "utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("route_map_invalid");
  const mappedForum = asNonEmpty(value.forum_chat_id)
    ?? asNonEmpty(value.groupChatId)
    ?? asNonEmpty(value.telegramForumChatId);
  if (mappedForum && mappedForum !== settings.forumChatId) throw new Error("route_map_forum_mismatch");
  const routes = value.routes && typeof value.routes === "object" && !Array.isArray(value.routes)
    ? value.routes
    : value.contactTopics;
  if (!routes || typeof routes !== "object" || Array.isArray(routes)) throw new Error("route_map_routes_required");
  const candidates = new Set();
  let matches = 0;
  for (const [rawIdentity, rawRoute] of Object.entries(routes)) {
    if (!rawRoute || typeof rawRoute !== "object" || Array.isArray(rawRoute) || rawRoute.enabled === false) continue;
    const routeThread = asNonEmpty(rawRoute.thread_id)
      ?? asNonEmpty(rawRoute.topic_id)
      ?? asNonEmpty(rawRoute.topicId);
    if (routeThread !== threadId) continue;
    matches += 1;
    const destination = canonicalWhatsappIdentity(
      rawRoute.whatsapp_target ?? rawRoute.whatsappTarget ?? rawIdentity,
    );
    if (!destination) throw new Error("route_identity_ambiguous");
    candidates.add(destination);
  }
  if (matches !== 1 || candidates.size !== 1) throw new Error("route_missing_or_ambiguous");
  return [...candidates][0];
}

function hashFile(filePath, maximumBytes = MAX_MEDIA_BYTES) {
  return new Promise((resolve, reject) => {
    const digest = createHash("sha256");
    let size = 0;
    let settled = false;
    const input = createReadStream(filePath);
    input.on("data", (chunk) => {
      size += chunk.length;
      if (size > maximumBytes) {
        input.destroy(new Error("media_too_large"));
        return;
      }
      digest.update(chunk);
    });
    input.once("error", (error) => {
      if (settled) return;
      settled = true;
      reject(error?.message === "media_too_large" ? error : new Error("media_read_failed"));
    });
    input.once("end", () => {
      if (settled) return;
      settled = true;
      resolve({ sha256: digest.digest("hex"), sizeBytes: size });
    });
  });
}

async function copyHumanMediaBounded(sourcePath, temporary, alreadyCopied) {
  let copied = 0;
  const limiter = new Transform({
    transform(chunk, _encoding, callback) {
      copied += chunk.length;
      if (copied > MAX_MEDIA_BYTES) return callback(new Error("media_too_large"));
      if (alreadyCopied + copied > MAX_TOTAL_MEDIA_BYTES) {
        return callback(new Error("media_total_too_large"));
      }
      callback(null, chunk);
    },
  });
  await pipeline(
    createReadStream(sourcePath),
    limiter,
    createWriteStream(temporary, { flags: "wx", mode: 0o600 }),
  );
  chmodSync(temporary, 0o600);
  const descriptor = openSync(temporary, "r+");
  try { fsyncSync(descriptor); } finally { closeSync(descriptor); }
  return copied;
}

async function inspectHumanMedia(event, requestId, text, settings) {
  const facts = officialMediaFacts(event);
  if (facts.length > MAX_MEDIA) throw new Error("media_count_exceeded");
  if (facts.length === 0) return { requestRoot: null, media: [], sources: [] };
  const roots = rootsFromEnvironment();
  if (roots.length === 0) throw new Error("media_roots_required");
  const sources = facts.map((fact) => {
    const rawPath = asNonEmpty(fact?.path);
    if (!rawPath) throw new Error("media_path_missing");
    const resolved = realpathSync(rawPath);
    if (!contained(resolved, roots) || !statSync(resolved).isFile()) throw new Error("media_path_rejected");
    const sizeBytes = statSync(resolved).size;
    if (sizeBytes > MAX_MEDIA_BYTES) throw new Error("media_too_large");
    return { fact, resolved, sizeBytes };
  });
  if (sources.reduce((total, item) => total + item.sizeBytes, 0) > MAX_TOTAL_MEDIA_BYTES) {
    throw new Error("media_total_too_large");
  }
  const requestRoot = path.join(settings.managedRoot, sha256(requestId));
  const media = [];
  for (let index = 0; index < sources.length; index += 1) {
    const { fact, resolved } = sources[index];
    const source = await hashFile(resolved, MAX_MEDIA_BYTES);
    const fileName = boundedFileName(
      fact?.fileName,
      `media-${index}${path.extname(resolved).slice(0, 16)}`,
    );
    const itemRoot = path.join(requestRoot, String(index));
    media.push({
      filePath: path.join(itemRoot, fileName),
      mediaType: kindOf(fact?.kind, resolved),
      mimeType: String(fact?.contentType ?? "").split(";", 1)[0].trim(),
      sha256: source.sha256,
      sizeBytes: source.sizeBytes,
      caption: index === 0 ? text : "",
      fileName,
    });
  }
  return { requestRoot, media, sources: sources.map((item) => item.resolved) };
}

function humanMediaDigest(media) {
  return media.map((item) => ({
    mediaType: item.mediaType,
    mimeType: item.mimeType,
    sha256: item.sha256,
    sizeBytes: item.sizeBytes,
    caption: item.caption,
    fileName: item.fileName,
  }));
}

async function stageHumanMedia(inspection, settings) {
  if (inspection.media.length === 0) return [];
  if (!contained(inspection.requestRoot, [settings.managedRoot])) {
    throw new Error("managed_media_request_root_invalid");
  }
  if (existsSync(inspection.requestRoot)) throw new Error("managed_media_collision");
  mkdirSync(inspection.requestRoot, { mode: 0o700 });
  const staged = [];
  let copiedTotal = 0;
  try {
    for (let index = 0; index < inspection.media.length; index += 1) {
      const descriptor = inspection.media[index];
      const resolved = inspection.sources[index];
      const itemRoot = path.dirname(descriptor.filePath);
      mkdirSync(itemRoot, { mode: 0o700 });
      const finalPath = descriptor.filePath;
      const temporary = path.join(itemRoot, ".media.tmp");
      if (existsSync(finalPath) || existsSync(temporary)) throw new Error("managed_media_collision");
      try {
        const copiedBytes = await copyHumanMediaBounded(resolved, temporary, copiedTotal);
        const copied = await hashFile(temporary, MAX_MEDIA_BYTES);
        copiedTotal += copiedBytes;
        if (copiedTotal > MAX_TOTAL_MEDIA_BYTES) throw new Error("media_total_too_large");
        if (copied.sha256 !== descriptor.sha256 || copied.sizeBytes !== descriptor.sizeBytes) {
          throw new Error("managed_media_hash_mismatch");
        }
        renameSync(temporary, finalPath);
        chmodSync(finalPath, 0o600);
      } catch (error) {
        try { unlinkSync(temporary); } catch {}
        try { unlinkSync(finalPath); } catch {}
        throw error;
      }
      staged.push(descriptor);
    }
    return staged;
  } catch (error) {
    try { rmSync(inspection.requestRoot, { recursive: true, force: true }); } catch {}
    throw error;
  }
}

function cleanupHumanMedia(job) {
  const requestRoot = asNonEmpty(job?.requestRoot);
  if (requestRoot && contained(requestRoot, [job.managedRoot]) && requestRoot !== job.managedRoot) {
    try { rmSync(requestRoot, { recursive: true, force: true }); } catch {}
    return;
  }
  for (const item of job.media ?? []) {
    const candidate = asNonEmpty(item?.filePath);
    if (!candidate) continue;
    try {
      const resolved = realpathSync(candidate);
      if (contained(resolved, [job.managedRoot])) unlinkSync(resolved);
    } catch {}
  }
}

async function sendNativeHumanOutbound(api, settings, job) {
  const loadAdapter = api?.runtime?.channel?.outbound?.loadAdapter;
  if (typeof loadAdapter !== "function") {
    throw Object.assign(new Error("whatsapp_outbound_loader_unavailable"), { proven: true });
  }
  const adapter = await loadAdapter("whatsapp");
  if (!adapter) {
    throw Object.assign(new Error("whatsapp_outbound_adapter_unavailable"), { proven: true });
  }
  const common = {
    cfg: api.config,
    to: job.destination,
    accountId: settings.whatsappAccountId,
  };
  const remoteMessageIds = [];
  try {
    if ((job.media ?? []).length === 0) {
      if (typeof adapter.sendText !== "function") {
        throw Object.assign(new Error("whatsapp_send_text_unavailable"), { proven: true });
      }
      const limit = Number.isSafeInteger(adapter.textChunkLimit) && adapter.textChunkLimit > 0
        ? adapter.textChunkLimit
        : null;
      const chunks = limit && typeof adapter.chunker === "function"
        ? adapter.chunker(job.text, limit)
        : [job.text];
      if (!Array.isArray(chunks) || chunks.length === 0) {
        throw Object.assign(new Error("whatsapp_text_chunking_failed"), { proven: true });
      }
      for (const chunk of chunks) {
        const result = await adapter.sendText({ ...common, text: chunk });
        const messageId = asNonEmpty(result?.messageId);
        if (!messageId) throw new Error("whatsapp_delivery_identity_missing");
        remoteMessageIds.push(messageId);
      }
      return remoteMessageIds;
    }
    if (typeof adapter.sendMedia !== "function") {
      throw Object.assign(new Error("whatsapp_send_media_unavailable"), { proven: true });
    }
    for (const item of job.media) {
      const result = await adapter.sendMedia({
        ...common,
        text: item.caption ?? "",
        mediaUrl: item.filePath,
        mediaLocalRoots: [settings.managedRoot],
        fileName: item.fileName,
        audioAsVoice: item.mediaType === "voice",
        forceDocument: item.mediaType === "document",
      });
      const messageId = asNonEmpty(result?.messageId);
      if (!messageId) throw new Error("whatsapp_delivery_identity_missing");
      remoteMessageIds.push(messageId);
    }
    return remoteMessageIds;
  } catch (error) {
    const failure = error instanceof Error ? error : new Error("whatsapp_delivery_failed");
    failure.remoteMessageIds = remoteMessageIds;
    throw failure;
  }
}

async function dispatchHumanOutbound(api, settings, job) {
  appendOutboundRecord(settings, {
    requestId: job.requestId,
    status: "dispatching",
    updatedAt: new Date().toISOString(),
  });
  try {
    const remoteMessageIds = await sendNativeHumanOutbound(api, settings, job);
    appendOutboundRecord(settings, {
      requestId: job.requestId,
      status: "sent",
      remoteMessageIds,
      updatedAt: new Date().toISOString(),
    });
    cleanupHumanMedia(job);
  } catch (error) {
    const status = error?.proven === true ? "failed" : "uncertain";
    appendOutboundRecord(settings, {
      requestId: job.requestId,
      status,
      errorCode: safeErrorCode(error),
      remoteMessageIds: Array.isArray(error?.remoteMessageIds) ? error.remoteMessageIds : [],
      updatedAt: new Date().toISOString(),
    });
    if (status === "failed") cleanupHumanMedia(job);
    throw error;
  }
}

function queueHumanOutbound(api, settings, job) {
  const current = outboundTail.then(() => dispatchHumanOutbound(api, settings, job));
  outboundTail = current.catch(() => undefined);
  return current;
}

async function captureHumanOutbound(api, event, ctx, settings, onDispatchFailure) {
  if (!settings || platformOf(event, ctx) !== "telegram") return false;
  const chatId = telegramChatId(event, ctx);
  if (chatId !== settings.forumChatId) return false;
  if (mediaStagingPending(event)) return true;
  const senderId = telegramSenderId(event, ctx);
  if (
    !senderId
    || !settings.allowedUsers.has(senderId)
    || isBotOrAssistant(event, ctx)
    || isServiceOrAutomation(event, ctx)
  ) return true;
  const threadId = telegramThreadId(event, ctx);
  const requestId = telegramOutboundRequestId(event, ctx);
  const destination = reverseRoute(settings, threadId);
  const rawText = typeof event?.content === "string" ? event.content : "";
  const text = officialMediaFacts(event).length > 0 && MEDIA_PLACEHOLDER.test(rawText.trim())
    ? ""
    : rawText;
  const inspection = await inspectHumanMedia(event, requestId, text, settings);
  if (!text.trim() && inspection.media.length === 0) throw new Error("outbound_content_required");
  const payloadSha256 = sha256(JSON.stringify({
    destination: sha256(destination),
    text,
    media: humanMediaDigest(inspection.media),
  }));
  const existing = settings.records.get(requestId);
  if (existing) {
    if (asNonEmpty(existing.payloadSha256) !== payloadSha256) {
      throw new Error("outbound_replay_conflict");
    }
    return true;
  }
  const media = await stageHumanMedia(inspection, settings);
  const job = {
    requestId,
    destination,
    text,
    media,
    managedRoot: settings.managedRoot,
    requestRoot: inspection.requestRoot,
  };
  appendOutboundRecord(settings, {
    requestId,
    status: "reserved",
    payloadSha256,
    job,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  });
  // Reservation and managed media are durable at this point. Do not hold the
  // pre-agent Telegram hook open while WhatsApp is slow or while earlier WIP=1
  // jobs drain. The serial tail owns delivery; gateway_start resumes any
  // reserved job after a process restart.
  queueHumanOutbound(api, settings, job).catch((error) => {
    if (typeof onDispatchFailure === "function") onDispatchFailure(error);
  });
  return true;
}

function resumeReservedHumanOutbound(api, settings) {
  if (!settings) return Promise.resolve();
  const reserved = [...settings.records.values()].filter((record) => record.status === "reserved" && record.job);
  return reserved.reduce(
    (tail, record) => tail.then(() => queueHumanOutbound(api, settings, record.job)),
    Promise.resolve(),
  );
}

export default definePluginEntry({
  id: PLUGIN_ID,
  name: "Espelho Zap Portable",
  description: "Pre-agent WhatsApp capture with bounded human topic outbound.",
  register(api) {
    const enabled = api?.config?.channels?.whatsapp?.pluginHooks?.messageReceived;
    if (enabled !== true) throw new Error("whatsapp_message_received_hook_required");
    const privacyScope = process.env.ESPELHO_ZAP_PRIVACY_SCOPE ?? "owner_private";
    if (!ALLOWED_SCOPES.has(privacyScope)) throw new Error("privacy_scope_required");
    const sourceProfileId = asNonEmpty(process.env.ESPELHO_ZAP_SOURCE_PROFILE_ID);
    if (!sourceProfileId) throw new Error("source_profile_required");
    const healthFile = asNonEmpty(process.env.ESPELHO_ZAP_HOOK_HEALTH_FILE);
    if (!healthFile || !path.isAbsolute(healthFile)) {
      throw new Error("hook_health_file_required");
    }
    const forumChatId = asNonEmpty(process.env.ESPELHO_ZAP_TELEGRAM_FORUM_CHAT_ID);
    const humanOutbound = humanOutboundSettings();
    if (humanOutbound && humanOutbound.forumChatId !== forumChatId) {
      throw new Error("human_outbound_forum_mismatch");
    }
    const pendingHumanOutbound = new Map();
    const pendingHumanOutboundBySession = new Map();
    async function capture(event, ctx, { requirePayload = false } = {}) {
      const payload = normalizeEvent(event, ctx, privacyScope, sourceProfileId);
      if (!payload) {
        if (requirePayload) throw new Error("capture_payload_missing");
        return false;
      }
      await queueIngest(payload);
      recordHealth(healthFile, true);
      return true;
    }
    function recordCaptureFailure(error) {
      const code = safeErrorCode(error);
      try { recordHealth(healthFile, false, code); } catch {}
      return code;
    }

    function forgetPendingHumanOutbound(requestId, entry) {
      if (pendingHumanOutbound.get(requestId) !== entry) return;
      pendingHumanOutbound.delete(requestId);
      if (entry.sessionKey) {
        const sessionRequests = pendingHumanOutboundBySession.get(entry.sessionKey);
        sessionRequests?.delete(requestId);
        if (sessionRequests?.size === 0) pendingHumanOutboundBySession.delete(entry.sessionKey);
      }
      if (entry.timer) clearTimeout(entry.timer);
    }

    function registerPendingHumanOutbound(event, ctx) {
      const requestId = telegramOutboundRequestId(event, ctx);
      const existing = pendingHumanOutbound.get(requestId);
      if (!existing && pendingHumanOutbound.size >= OUTBOUND_CAPTURE_PENDING_MAX) {
        throw new Error("human_outbound_capture_capacity_exceeded");
      }
      if (existing) forgetPendingHumanOutbound(requestId, existing);
      const sessionKey = asNonEmpty(ctx?.sessionKey) ?? asNonEmpty(event?.sessionKey);
      // Schedule capture in a microtask so the Promise is indexed before any
      // hashing, copy or other await can yield back to OpenClaw's pre-model gate.
      // A duplicate update is chained after the prior validation instead of
      // merely reusing its Promise: the durable ledger must still compare the
      // new payload digest and reject a same-id/different-payload conflict.
      const predecessor = existing?.promise.catch(() => undefined) ?? Promise.resolve();
      const promise = predecessor.then(() =>
        captureHumanOutbound(api, event, ctx, humanOutbound, recordCaptureFailure),
      );
      const entry = { promise, requestId, sessionKey, timer: null };
      pendingHumanOutbound.set(requestId, entry);
      if (sessionKey) {
        const sessionRequests = pendingHumanOutboundBySession.get(sessionKey) ?? new Set();
        sessionRequests.add(requestId);
        pendingHumanOutboundBySession.set(sessionKey, sessionRequests);
      }
      entry.timer = setTimeout(
        () => forgetPendingHumanOutbound(requestId, entry),
        OUTBOUND_CAPTURE_RETENTION_MS,
      );
      entry.timer.unref?.();
      return entry;
    }

    function pendingHumanOutboundForTurn(event, ctx) {
      const entries = new Set();
      try {
        const exact = pendingHumanOutbound.get(telegramOutboundRequestId(event, ctx));
        if (exact) entries.add(exact);
      } catch {}
      const sessionKey = asNonEmpty(ctx?.sessionKey) ?? asNonEmpty(event?.sessionKey);
      if (sessionKey) {
        for (const requestId of pendingHumanOutboundBySession.get(sessionKey) ?? []) {
          const entry = pendingHumanOutbound.get(requestId);
          if (entry) entries.add(entry);
        }
      }
      return [...entries];
    }

    // A bound OpenClaw conversation can be claimed here. Capture is awaited
    // before handled=true silently terminates the inbound turn. OpenClaw may
    // expose an early media fact with staging pending; that pass is deliberately
    // not claimed so the later staged message_received event can capture it.
    // Capture failure is otherwise fail-closed and cannot reach an agent.
    api.on("inbound_claim", async (event, ctx) => {
      if (platformOf(event, ctx) !== "whatsapp") return undefined;
      if (mediaStagingPending(event)) return undefined;
      try {
        await capture(event, ctx, { requirePayload: true });
      } catch (error) {
        recordCaptureFailure(error);
      }
      return { handled: true };
    });

    // Global/unbound channel fallback. OpenClaw fires message_received before
    // normal agent processing. Telegram messages in the exact mirror forum are
    // consumed as human transport intent; WhatsApp remains passive capture.
    api.on("message_received", async (event, ctx) => {
      if (forumChatId && platformOf(event, ctx) === "telegram" && telegramChatId(event, ctx) === forumChatId) {
        if (mediaStagingPending(event)) return undefined;
        try {
          // OpenClaw runs message_received as fire-and-forget. Register the
          // Promise synchronously; before_agent_reply will await this exact
          // reservation before the Telegram update may finish silently.
          const entry = registerPendingHumanOutbound(event, ctx);
          await entry.promise;
          return undefined;
        } catch (error) {
          recordCaptureFailure(error);
          return undefined;
        }
      }
      try {
        await capture(event, ctx);
        return undefined;
      } catch (error) {
        const code = recordCaptureFailure(error);
        // OpenClaw records a rejected hook and continues. Do not swallow the
        // capture error: persisted aggregate health and runtime logs agree.
        // Never rethrow Node errors that may contain a local media path.
        throw new Error(code);
      }
    });

    // If the inbound was not plugin-bound, stop it before the agent produces a
    // reply. Omitting `reply` is the official silent handled result.
    api.on("before_agent_reply", (event, ctx) => {
      if (forumChatId && platformOf(event, ctx) === "telegram" && telegramChatId(event, ctx) === forumChatId) {
        const handled = { handled: true, reason: FORUM_DATA_PLANE_REASON };
        const entries = pendingHumanOutboundForTurn(event, ctx);
        if (entries.length === 0) return handled;
        return Promise.allSettled(entries.map((entry) => entry.promise)).then(() => {
          for (const entry of entries) forgetPendingHumanOutbound(entry.requestId, entry);
          return handled;
        });
      }
      if (platformOf(event, ctx) !== "whatsapp") return undefined;
      return { handled: true, reason: PASSIVE_REASON };
    });

    // Defense in depth for every OpenClaw delivery path: no WhatsApp outbound
    // is permitted even if an upstream routing regression bypasses the silent
    // claim/reply gate.
    api.on("message_sending", (event, ctx) => {
      if (platformOf(event, ctx) !== "whatsapp") return undefined;
      return { cancel: true, cancelReason: PASSIVE_REASON };
    });
    api.on("reply_payload_sending", (event, ctx) => {
      if (platformOf(event, ctx) !== "whatsapp") return undefined;
      return { cancel: true, reason: PASSIVE_REASON };
    });

    // A reservation that committed before a clean Gateway restart is safe to
    // resume. A dispatch that had already begun was converted to `uncertain`
    // while loading the ledger and is never replayed blindly.
    api.on("gateway_start", async () => {
      try {
        await resumeReservedHumanOutbound(api, humanOutbound);
      } catch (error) {
        recordCaptureFailure(error);
      }
    });
  },
});
