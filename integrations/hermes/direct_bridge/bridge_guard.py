#!/usr/bin/env python3
"""Versioned compatibility guard for Hermes' external Baileys bridge.

The product keeps Hermes' generic WhatsApp mutation routes disabled while the
bridge runs observe-only.  This patch adds one narrowly authenticated,
loopback transport route for *human* replies captured from an exact Telegram
forum topic: ``POST /mirror-human-send``.  The injected route validates its
token, target, media root, file identity, size and optional digest before it
uses the bridge's existing serialized ``sendManualWithTimeout`` primitive.

The patch never carries, copies or inspects a WhatsApp session.  Version 4
adds an authenticated, read-only loopback health route, records only
provider-backed outbound receipt events, and upgrades the former V3/V2/V1
middleware in place.  Receipt state is never inferred from HTTP success: when
the provider emits no receipt, the ledger remains honestly at ``sent``.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import tempfile


GUARD_VERSION = 4
GUARD_MARKER = "ESPELHO_ZAP_HUMAN_OUTBOUND_COMPAT_GUARD_V4"
PRIOR_GUARD_MARKER = "ESPELHO_ZAP_HUMAN_OUTBOUND_COMPAT_GUARD_V3"
OLDER_GUARD_MARKER = "ESPELHO_ZAP_HUMAN_OUTBOUND_COMPAT_GUARD_V2"
LEGACY_GUARD_MARKER = "ESPELHO_ZAP_OBSERVE_ONLY_COMPAT_GUARD_V1"
_EXPRESS_ANCHOR = "app.use(express.json());"
_MANUAL_ROUTE = re.compile(r"app\.post\(\s*['\"]\/mirror-manual-send['\"]")
_SEND_ROUTE = re.compile(r"app\.post\(\s*['\"]\/send['\"]")
_SEND_MEDIA_ROUTE = re.compile(r"app\.post\(\s*['\"]\/send-media['\"]")
_OBSERVE_ONLY = re.compile(r"\b(?:const|let|var)\s+OBSERVE_ONLY\b")
_SOCK_BINDING = re.compile(r"(?m)^(?:const|let|var)\s+sock\b")
_CONNECTION_STATE_BINDING = re.compile(
    r"(?m)^(?:const|let|var)\s+connectionState\b"
)
_SERIAL_SEND = re.compile(r"\bfunction\s+sendManualWithTimeout\s*\(")
_TRACK_SENT = re.compile(r"\bfunction\s+trackSentMessageId\s*\(")

_RECEIPT_HOOK_OLD = r"""
// Provider-backed receipts are captured as durable spool events.  They never
// infer delivery from HTTP success and they only cover message ids tracked by
// the human outbound path.
function _espelhoReceiptState(receipt, update) {
  const raw = receipt?.status ?? receipt?.state ?? receipt?.type
    ?? update?.status ?? update?.state;
  if (Number.isInteger(raw) && raw >= 2 && raw <= 5) return raw;
  const aliases = {
    sent: 2, device: 2, delivered: 3, read: 4, played: 5,
  };
  if (typeof raw === 'string' && aliases[raw.trim().toLowerCase()]) {
    return aliases[raw.trim().toLowerCase()];
  }
  if (receipt?.playedTimestamp || update?.playedTimestamp) return 5;
  if (receipt?.readTimestamp || update?.readTimestamp) return 4;
  if (receipt?.receiptTimestamp || update?.receiptTimestamp) return 3;
  return 0;
}

function _espelhoQueueOutboundReceipt(providerEvent, item) {
  if (typeof messageSpool === 'undefined' || typeof messageSpool?.add !== 'function') return;
  if (typeof recentlySentIds === 'undefined' || typeof recentlySentIds?.has !== 'function') return;
  const key = item?.key || {};
  const messageId = String(key?.id || '').trim();
  if (!messageId || !recentlySentIds.has(messageId)) return;
  const receipt = item?.receipt || item?.update || {};
  const update = item?.update || {};
  const state = _espelhoReceiptState(receipt, update);
  if (!state) return;
  const eventId = `receipt:${messageId}:${providerEvent}:${state}`;
  messageSpool.add({
    messageId: eventId,
    nativeType: 'outbound_receipt',
    nativeMetadata: {
      receipt: {
        outboundMessageId: messageId,
        state,
        providerEvent,
      },
    },
  });
}

if (typeof sock !== 'undefined' && sock?.ev?.on
    && typeof messageSpool !== 'undefined') {
  sock.ev.on('message-receipt.update', (updates) => {
    const rows = Array.isArray(updates) ? updates : [updates];
    for (const item of rows) _espelhoQueueOutboundReceipt('message-receipt.update', item);
  });
  sock.ev.on('messages.update', (updates) => {
    const rows = Array.isArray(updates) ? updates : [updates];
    for (const item of rows) {
      if (item?.update?.status !== undefined || item?.status !== undefined) {
        _espelhoQueueOutboundReceipt('messages.update', item);
      }
    }
  });
}
"""

_RECEIPT_HOOK = r"""
// Provider-backed receipts are captured as durable spool events.  They never
// infer delivery from HTTP success and they only cover message ids tracked by
// the human outbound path. Baileys creates and replaces `sock` asynchronously,
// so registration follows each live socket instead of running only at module
// load time.
function _espelhoReceiptState(receipt, update) {
  const raw = receipt?.status ?? receipt?.state ?? receipt?.type
    ?? update?.status ?? update?.state;
  if (Number.isInteger(raw) && raw >= 2 && raw <= 5) return raw;
  const aliases = {
    sent: 2, device: 2, delivered: 3, read: 4, played: 5,
  };
  if (typeof raw === 'string' && aliases[raw.trim().toLowerCase()]) {
    return aliases[raw.trim().toLowerCase()];
  }
  if (receipt?.playedTimestamp || update?.playedTimestamp) return 5;
  if (receipt?.readTimestamp || update?.readTimestamp) return 4;
  if (receipt?.receiptTimestamp || update?.receiptTimestamp) return 3;
  return 0;
}

function _espelhoQueueOutboundReceipt(providerEvent, item) {
  if (typeof messageSpool === 'undefined' || typeof messageSpool?.add !== 'function') return;
  if (typeof recentlySentIds === 'undefined' || typeof recentlySentIds?.has !== 'function') return;
  const key = item?.key || {};
  const messageId = String(key?.id || '').trim();
  if (!messageId || !recentlySentIds.has(messageId)) return;
  const receipt = item?.receipt || item?.update || {};
  const update = item?.update || {};
  const state = _espelhoReceiptState(receipt, update);
  if (!state) return;
  const eventId = `receipt:${messageId}:${providerEvent}:${state}`;
  messageSpool.add({
    messageId: eventId,
    nativeType: 'outbound_receipt',
    nativeMetadata: {
      receipt: {
        outboundMessageId: messageId,
        state,
        providerEvent,
      },
    },
  });
}

const _espelhoReceiptSockets = new WeakSet();
function _espelhoInstallReceiptHooks(candidateSock) {
  if (!candidateSock?.ev?.on || typeof messageSpool === 'undefined'
      || typeof messageSpool?.add !== 'function'
      || _espelhoReceiptSockets.has(candidateSock)) return;
  _espelhoReceiptSockets.add(candidateSock);
  candidateSock.ev.on('message-receipt.update', (updates) => {
    const rows = Array.isArray(updates) ? updates : [updates];
    for (const item of rows) _espelhoQueueOutboundReceipt('message-receipt.update', item);
  });
  candidateSock.ev.on('messages.update', (updates) => {
    const rows = Array.isArray(updates) ? updates : [updates];
    for (const item of rows) {
      if (item?.update?.status !== undefined || item?.status !== undefined) {
        _espelhoQueueOutboundReceipt('messages.update', item);
      }
    }
  });
}

if (typeof setInterval === 'function') {
  const _espelhoReceiptHookTimer = setInterval(() => {
    try {
      _espelhoInstallReceiptHooks(typeof sock !== 'undefined' ? sock : null);
    } catch {}
  }, 250);
  if (typeof _espelhoReceiptHookTimer?.unref === 'function') {
    _espelhoReceiptHookTimer.unref();
  }
}
"""

_LEGACY_GUARD = f"""

// {LEGACY_GUARD_MARKER}
// The portable mirror is inbound-only.  This runs before every route and
// blocks the historical manual outbound endpoint even when a stale token is
// still present on disk or inherited through the environment.
app.use((req, res, next) => {{
  if (OBSERVE_ONLY && req.method === 'POST' && req.path === '/mirror-manual-send') {{
    return res.status(405).json({{ error: 'WhatsApp outbound disabled: observe-only mode' }});
  }}
  next();
}});
"""

_GUARD_TEMPLATE_V2 = r"""

// __GUARD_MARKER__
// Human outbound is deliberately narrower than Hermes' generic send API:
// one authenticated mapped contact/group route, exact media confinement and
// one serialized request at a time. Generic/legacy mutation routes stay shut.
const _ESPELHO_HUMAN_ROUTE = '/mirror-human-send';
const _ESPELHO_HUMAN_TOKEN_FILE = String(
  process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE || ''
).trim();
const _ESPELHO_HUMAN_MEDIA_ROOT = String(
  process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_MEDIA_ROOT || ''
).trim();
const _ESPELHO_HUMAN_MAX_MEDIA_BYTES = Math.max(
  1,
  Number.parseInt(process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_MAX_MEDIA_BYTES || '134217728', 10)
    || 134217728,
);
const _ESPELHO_HUMAN_MAX_TOTAL_MEDIA_BYTES = Math.max(
  _ESPELHO_HUMAN_MAX_MEDIA_BYTES,
  Number.parseInt(process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_MAX_TOTAL_MEDIA_BYTES || '268435456', 10)
    || 268435456,
);
const _ESPELHO_HUMAN_MEDIA_TYPES = new Set([
  'image', 'audio', 'voice', 'video', 'document',
]);
const _ESPELHO_BLOCKED_OUTBOUND_ROUTES = new Set([
  '/mirror-manual-send',
  '/send',
  '/send-media',
  '/edit',
  '/send-location',
  '/typing',
  '/send-poll',
  '/send-reaction',
]);
const _espelhoHumanRequestResults = new Map();
let _espelhoHumanRequestQueue = Promise.resolve();

function _espelhoHumanError(status, code) {
  const error = new Error(code);
  error.httpStatus = status;
  error.publicCode = code;
  error.attempted = false;
  return error;
}

function _espelhoQueueHumanRequest(fn) {
  const task = _espelhoHumanRequestQueue.then(() => fn(), () => fn());
  _espelhoHumanRequestQueue = task.catch(() => {});
  return task;
}

function _espelhoRememberHumanResult(requestId, fingerprint, httpStatus, body) {
  _espelhoHumanRequestResults.set(requestId, { fingerprint, httpStatus, body });
  while (_espelhoHumanRequestResults.size > 1000) {
    const oldest = _espelhoHumanRequestResults.keys().next().value;
    _espelhoHumanRequestResults.delete(oldest);
  }
}

async function _espelhoHumanTokenMatches(value) {
  if (typeof value !== 'string' || !_ESPELHO_HUMAN_TOKEN_FILE) return false;
  try {
    const fs = await import('node:fs');
    const pathModule = await import('node:path');
    const crypto = await import('node:crypto');
    if (!pathModule.isAbsolute(_ESPELHO_HUMAN_TOKEN_FILE)) return false;
    const details = fs.lstatSync(_ESPELHO_HUMAN_TOKEN_FILE);
    if (!details.isFile() || details.isSymbolicLink()
        || (process.platform !== 'win32' && (details.mode & 0o077) !== 0)) return false;
    const expectedText = fs.readFileSync(_ESPELHO_HUMAN_TOKEN_FILE, 'utf8').trim();
    if (expectedText.length < 43) return false;
    const presented = Buffer.from(value, 'utf8');
    const expected = Buffer.from(expectedText, 'utf8');
    return presented.length === expected.length && crypto.timingSafeEqual(presented, expected);
  } catch {
    return false;
  }
}

function _espelhoCleanOptionalString(value, maximum, code) {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value !== 'string' || value.length > maximum || /[\u0000]/.test(value)) {
    throw _espelhoHumanError(400, code);
  }
  return value;
}

function _espelhoHumanRequestIsLoopback(req) {
  const remote = String(req.socket?.remoteAddress || '').toLowerCase();
  const loopbackRemote = remote === '127.0.0.1' || remote === '::1'
    || remote === '::ffff:127.0.0.1';
  const rawHost = String(req.headers?.host || '').trim().toLowerCase();
  const hostWithoutPort = rawHost.startsWith('[')
    ? rawHost.slice(1, rawHost.indexOf(']'))
    : rawHost.split(':', 1)[0];
  return loopbackRemote && new Set(['localhost', '127.0.0.1', '::1']).has(hostWithoutPort);
}

async function _espelhoInspectHumanMedia(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) {
    throw _espelhoHumanError(400, 'invalid_media');
  }
  const fs = await import('node:fs');
  const pathModule = await import('node:path');
  if (!_ESPELHO_HUMAN_MEDIA_ROOT || !pathModule.isAbsolute(_ESPELHO_HUMAN_MEDIA_ROOT)) {
    throw _espelhoHumanError(503, 'media_root_unavailable');
  }
  const rootDetails = fs.lstatSync(_ESPELHO_HUMAN_MEDIA_ROOT);
  if (!rootDetails.isDirectory() || rootDetails.isSymbolicLink()
      || (process.platform !== 'win32' && (rootDetails.mode & 0o077) !== 0)) {
    throw _espelhoHumanError(503, 'media_root_unavailable');
  }
  if (typeof item.filePath !== 'string' || !pathModule.isAbsolute(item.filePath)) {
    throw _espelhoHumanError(400, 'invalid_media_path');
  }
  const rootReal = fs.realpathSync(_ESPELHO_HUMAN_MEDIA_ROOT);
  const candidateDetails = fs.lstatSync(item.filePath);
  if (!candidateDetails.isFile() || candidateDetails.isSymbolicLink()) {
    throw _espelhoHumanError(400, 'invalid_media_path');
  }
  const candidateReal = fs.realpathSync(item.filePath);
  const relative = pathModule.relative(rootReal, candidateReal);
  if (!relative || relative.startsWith(`..${pathModule.sep}`) || pathModule.isAbsolute(relative)) {
    throw _espelhoHumanError(400, 'media_outside_managed_root');
  }
  if (candidateDetails.size > _ESPELHO_HUMAN_MAX_MEDIA_BYTES) {
    throw _espelhoHumanError(413, 'media_too_large');
  }
  if (item.sizeBytes !== undefined && item.sizeBytes !== null
      && (!Number.isSafeInteger(item.sizeBytes) || item.sizeBytes < 0
        || item.sizeBytes !== candidateDetails.size)) {
    throw _espelhoHumanError(409, 'media_size_mismatch');
  }
  if (item.sha256 !== undefined && item.sha256 !== null && item.sha256 !== '') {
    if (typeof item.sha256 !== 'string' || !/^[a-fA-F0-9]{64}$/.test(item.sha256)) {
      throw _espelhoHumanError(400, 'invalid_media_sha256');
    }
  }
  if (typeof item.mediaType !== 'string' || !_ESPELHO_HUMAN_MEDIA_TYPES.has(item.mediaType)) {
    throw _espelhoHumanError(400, 'unsupported_media_type');
  }
  const mimeType = _espelhoCleanOptionalString(item.mimeType, 255, 'invalid_mime_type');
  if (/[\r\n]/.test(mimeType)) throw _espelhoHumanError(400, 'invalid_mime_type');
  const caption = _espelhoCleanOptionalString(item.caption, 4096, 'invalid_caption');
  const requestedName = _espelhoCleanOptionalString(item.fileName, 255, 'invalid_file_name');
  const fileName = requestedName ? pathModule.basename(requestedName) : pathModule.basename(candidateReal);
  return {
    filePath: candidateReal,
    device: candidateDetails.dev,
    inode: candidateDetails.ino,
    mtimeMs: candidateDetails.mtimeMs,
    mediaType: item.mediaType,
    mimeType,
    caption,
    fileName,
    expectedSha256: typeof item.sha256 === 'string' ? item.sha256.toLowerCase() : '',
    sizeBytes: candidateDetails.size,
  };
}

async function _espelhoReadHumanMedia(inspected) {
  const fs = await import('node:fs');
  const crypto = await import('node:crypto');
  const openFlags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0);
  const descriptor = fs.openSync(inspected.filePath, openFlags);
  let before;
  let after;
  let buffer;
  try {
    before = fs.fstatSync(descriptor);
    if (!before.isFile() || before.size > _ESPELHO_HUMAN_MAX_MEDIA_BYTES
        || before.dev !== inspected.device || before.ino !== inspected.inode
        || before.size !== inspected.sizeBytes || before.mtimeMs !== inspected.mtimeMs) {
      throw _espelhoHumanError(409, 'media_changed_before_read');
    }
    buffer = fs.readFileSync(descriptor);
    after = fs.fstatSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
  if (before.dev !== after.dev || before.ino !== after.ino || before.size !== after.size
      || before.mtimeMs !== after.mtimeMs || buffer.length !== after.size) {
    throw _espelhoHumanError(409, 'media_changed_during_read');
  }
  const actualSha256 = crypto.createHash('sha256').update(buffer).digest('hex');
  if (inspected.expectedSha256 && inspected.expectedSha256 !== actualSha256) {
    throw _espelhoHumanError(409, 'media_sha256_mismatch');
  }
  return { ...inspected, buffer, sha256: actualSha256 };
}

function _espelhoHumanMediaPayload(media, caption) {
  const mimetype = media.mimeType || undefined;
  switch (media.mediaType) {
    case 'image':
      return { image: media.buffer, caption: caption || undefined, mimetype };
    case 'video':
      return { video: media.buffer, caption: caption || undefined, mimetype };
    case 'audio':
      return { audio: media.buffer, mimetype: mimetype || 'audio/mpeg', ptt: false };
    case 'voice':
      return { audio: media.buffer, mimetype: mimetype || 'audio/ogg; codecs=opus', ptt: true };
    case 'document':
      return {
        document: media.buffer,
        fileName: media.fileName,
        caption: caption || undefined,
        mimetype: mimetype || 'application/octet-stream',
      };
    default:
      throw _espelhoHumanError(400, 'unsupported_media_type');
  }
}

app.post(_ESPELHO_HUMAN_ROUTE, async (req, res) => {
  if (!OBSERVE_ONLY) {
    return res.status(409).json({ error: 'observe_only_required' });
  }
  if (!_espelhoHumanRequestIsLoopback(req)) {
    return res.status(403).json({ error: 'loopback_required' });
  }
  if (!(await _espelhoHumanTokenMatches(req.get('x-espelho-token')))) {
    return res.status(403).json({ error: 'forbidden' });
  }
  const requestId = typeof req.body?.requestId === 'string' ? req.body.requestId.trim() : '';
  const chatId = typeof req.body?.chatId === 'string' ? req.body.chatId.trim() : '';
  const text = req.body?.text === undefined || req.body?.text === null ? '' : req.body.text;
  const media = req.body?.media === undefined ? [] : req.body.media;
  if (!/^[A-Za-z0-9._:-]{1,160}$/.test(requestId)) {
    return res.status(400).json({ error: 'invalid_request_id' });
  }
  if (!/^(?:\d{6,20}@s\.whatsapp\.net|\d[\d-]{5,39}@g\.us)$/.test(chatId)) {
    return res.status(400).json({ error: 'mapped_chat_id_required' });
  }
  if (typeof text !== 'string' || text.length > 4096 || !Array.isArray(media) || media.length > 8) {
    return res.status(400).json({ error: 'invalid_payload' });
  }
  if (!text.trim() && media.length === 0) {
    return res.status(400).json({ error: 'text_or_media_required' });
  }
  const crypto = await import('node:crypto');
  const fingerprint = crypto.createHash('sha256').update(JSON.stringify(req.body)).digest('hex');
  const existing = _espelhoHumanRequestResults.get(requestId);
  if (existing) {
    if (existing.fingerprint !== fingerprint) {
      return res.status(409).json({ error: 'request_id_conflict' });
    }
    return res.status(existing.httpStatus).json(existing.body);
  }
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'whatsapp_unavailable', attempted: false });
  }
  try {
    const outcome = await _espelhoQueueHumanRequest(async () => {
      const raced = _espelhoHumanRequestResults.get(requestId);
      if (raced) {
        if (raced.fingerprint !== fingerprint) {
          return { httpStatus: 409, body: { error: 'request_id_conflict' } };
        }
        return raced;
      }
      const inspectedMedia = [];
      let totalMediaBytes = 0;
      for (const item of media) {
        const inspected = await _espelhoInspectHumanMedia(item);
        totalMediaBytes += inspected.sizeBytes;
        if (totalMediaBytes > _ESPELHO_HUMAN_MAX_TOTAL_MEDIA_BYTES) {
          throw _espelhoHumanError(413, 'media_total_too_large');
        }
        inspectedMedia.push(inspected);
      }
      const messageIds = [];
      let deliveryStarted = false;
      try {
        // Telegram repeats a media caption in event.text. Never send it twice:
        // text is a standalone WhatsApp message only when media is empty.
        if (inspectedMedia.length === 0) {
          const { content: payload, options } = buildTextSendPayload(text, { chatId, messageStore });
          deliveryStarted = true;
          const sent = await sendManualWithTimeout(chatId, payload, options);
          const messageId = String(sent?.key?.id || '').trim();
          if (!messageId) throw new Error('missing_message_id');
          trackSentMessageId(sent);
          messageStore.remember(sent);
          messageIds.push(messageId);
        } else {
          for (let index = 0; index < inspectedMedia.length; index += 1) {
            // Read, validate and send one file at a time.  Never retain an
            // album worth of Buffers in the bridge process.
            const item = await _espelhoReadHumanMedia(inspectedMedia[index]);
            const caption = index === 0 ? item.caption : '';
            deliveryStarted = true;
            const sent = await sendManualWithTimeout(
              chatId,
              _espelhoHumanMediaPayload(item, caption),
            );
            const messageId = String(sent?.key?.id || '').trim();
            if (!messageId) throw new Error('missing_message_id');
            trackSentMessageId(sent);
            messageStore.remember(sent);
            messageIds.push(messageId);
          }
        }
      } catch (error) {
        if (!deliveryStarted && error?.attempted === false) throw error;
        const body = {
          error: 'delivery_outcome_uncertain',
          uncertain: true,
          requestId,
          messageIds,
        };
        _espelhoRememberHumanResult(requestId, fingerprint, 502, body);
        return { fingerprint, httpStatus: 502, body };
      }
      const body = {
        success: true,
        requestId,
        messageId: messageIds[messageIds.length - 1] || null,
        messageIds,
      };
      _espelhoRememberHumanResult(requestId, fingerprint, 200, body);
      return { fingerprint, httpStatus: 200, body };
    });
    return res.status(outcome.httpStatus).json(outcome.body);
  } catch (error) {
    const status = Number.isInteger(error?.httpStatus) ? error.httpStatus : 500;
    const code = typeof error?.publicCode === 'string' ? error.publicCode : 'human_outbound_failed';
    const body = { error: code, requestId };
    if (error?.attempted === false) body.attempted = false;
    return res.status(status).json(body);
  }
});

app.use((req, res, next) => {
  let normalizedPath = String(req.path || '');
  try { normalizedPath = decodeURIComponent(normalizedPath); } catch {}
  normalizedPath = normalizedPath.replace(/\/+$/, '') || '/';
  if (OBSERVE_ONLY && req.method === 'POST' && _ESPELHO_BLOCKED_OUTBOUND_ROUTES.has(normalizedPath)) {
    return res.status(405).json({ error: 'WhatsApp outbound disabled: observe-only mode' });
  }
  next();
});
"""

_HEALTH_CONSTANT = "const _ESPELHO_HUMAN_HEALTH_ROUTE = '/mirror-human-health';"
_HEALTH_ROUTE_V3 = r"""
app.get(_ESPELHO_HUMAN_HEALTH_ROUTE, async (req, res) => {
  if (!_espelhoHumanRequestIsLoopback(req)) {
    return res.status(403).json({ error: 'loopback_required' });
  }
  if (!(await _espelhoHumanTokenMatches(req.get('x-espelho-token')))) {
    return res.status(403).json({ error: 'forbidden' });
  }
  return res.status(200).json({
    schemaVersion: 1,
    guardVersion: 3,
    observeOnly: Boolean(OBSERVE_ONLY),
    connectionState: String(connectionState || ''),
    connected: Boolean(sock && connectionState === 'connected'),
  });
});

"""
_HEALTH_ROUTE = _HEALTH_ROUTE_V3.replace("guardVersion: 3", "guardVersion: 4", 1)
_GUARD_V3 = (
    _GUARD_TEMPLATE_V2.replace("__GUARD_MARKER__", PRIOR_GUARD_MARKER)
    .replace(
        "const _ESPELHO_HUMAN_ROUTE = '/mirror-human-send';",
        "const _ESPELHO_HUMAN_ROUTE = '/mirror-human-send';\n" + _HEALTH_CONSTANT,
        1,
    )
    .replace(
        "app.post(_ESPELHO_HUMAN_ROUTE, async (req, res) => {",
        _HEALTH_ROUTE_V3 + "app.post(_ESPELHO_HUMAN_ROUTE, async (req, res) => {",
        1,
    )
)
_GUARD_V2 = _GUARD_TEMPLATE_V2.replace("__GUARD_MARKER__", OLDER_GUARD_MARKER)
_GUARD = (
    _GUARD_TEMPLATE_V2.replace("__GUARD_MARKER__", GUARD_MARKER)
    .replace(
        "const _ESPELHO_HUMAN_ROUTE = '/mirror-human-send';",
        "const _ESPELHO_HUMAN_ROUTE = '/mirror-human-send';\n" + _HEALTH_CONSTANT,
        1,
    )
    .replace(
        "app.post(_ESPELHO_HUMAN_ROUTE, async (req, res) => {",
        _HEALTH_ROUTE + _RECEIPT_HOOK + "app.post(_ESPELHO_HUMAN_ROUTE, async (req, res) => {",
        1,
    )
)
_GUARD_V4_OLD = (
    _GUARD_TEMPLATE_V2.replace("__GUARD_MARKER__", GUARD_MARKER)
    .replace(
        "const _ESPELHO_HUMAN_ROUTE = '/mirror-human-send';",
        "const _ESPELHO_HUMAN_ROUTE = '/mirror-human-send';\n" + _HEALTH_CONSTANT,
        1,
    )
    .replace(
        "app.post(_ESPELHO_HUMAN_ROUTE, async (req, res) => {",
        _HEALTH_ROUTE + _RECEIPT_HOOK_OLD + "app.post(_ESPELHO_HUMAN_ROUTE, async (req, res) => {",
        1,
    )
)


class BridgeGuardError(ValueError):
    """Raised when a bridge cannot be guarded without guessing its layout."""


def _assert_no_symlink_components(path: Path, *, allow_missing_leaf: bool = False) -> None:
    candidate = path
    if allow_missing_leaf and not candidate.exists() and not candidate.is_symlink():
        candidate = candidate.parent
    while True:
        if candidate.is_symlink():
            raise BridgeGuardError("bridge and backup paths must not contain symlinks")
        if candidate == candidate.parent:
            return
        candidate = candidate.parent


def _route_offset(source: str) -> int:
    match = _MANUAL_ROUTE.search(source)
    return -1 if match is None else match.start()


def bridge_is_guarded(source: str) -> bool:
    marker = source.find(GUARD_MARKER)
    route = _route_offset(source)
    human_route = source.find("app.post(_ESPELHO_HUMAN_ROUTE")
    return (
        marker >= 0
        and source.count(GUARD_MARKER) == 1
        and source.count(_GUARD) == 1
        and human_route > marker
        and route > human_route
        and PRIOR_GUARD_MARKER not in source
        and OLDER_GUARD_MARKER not in source
        and LEGACY_GUARD_MARKER not in source
    )


def validate_compatible_source(source: str) -> None:
    if not _OBSERVE_ONLY.search(source):
        raise BridgeGuardError("bridge is missing the OBSERVE_ONLY safety state")
    if _route_offset(source) < 0:
        raise BridgeGuardError("bridge is missing the manual outbound route")
    if not _SEND_ROUTE.search(source) or not _SEND_MEDIA_ROUTE.search(source):
        raise BridgeGuardError("bridge is missing the generic outbound routes")
    if not _SERIAL_SEND.search(source):
        raise BridgeGuardError("bridge is missing sendManualWithTimeout")
    if not _TRACK_SENT.search(source):
        raise BridgeGuardError("bridge is missing sent-message tracking")
    express_offset = source.find(_EXPRESS_ANCHOR)
    sock_binding = _SOCK_BINDING.search(source)
    connection_binding = _CONNECTION_STATE_BINDING.search(source)
    if (
        sock_binding is None
        or connection_binding is None
        or express_offset < 0
        or sock_binding.start() >= express_offset
        or connection_binding.start() >= express_offset
    ):
        raise BridgeGuardError(
            "bridge is missing top-level socket or connection-state bindings"
        )
    if "buildTextSendPayload" not in source or "messageStore" not in source:
        raise BridgeGuardError("bridge is missing text payload or message-store support")
    if source.count(_EXPRESS_ANCHOR) != 1:
        raise BridgeGuardError("bridge has no unique Express JSON middleware anchor")


def patch_bridge_source(source: str) -> str:
    validate_compatible_source(source)
    if bridge_is_guarded(source):
        return source
    if GUARD_MARKER in source:
        if source.count(_GUARD_V4_OLD) == 1:
            upgraded = source.replace(_GUARD_V4_OLD, _GUARD, 1)
            if not bridge_is_guarded(upgraded):
                raise BridgeGuardError(
                    "bridge V4 receipt-hook upgrade did not produce a complete guard"
                )
            return upgraded
        raise BridgeGuardError("bridge contains an incomplete V3 guard")
    if PRIOR_GUARD_MARKER in source:
        if source.count(PRIOR_GUARD_MARKER) != 1 or source.count(_GUARD_V3) != 1:
            raise BridgeGuardError("bridge contains an unrecognized V3 guard")
        upgraded = source.replace(_GUARD_V3, _GUARD, 1)
        if not bridge_is_guarded(upgraded):
            raise BridgeGuardError("bridge V3 upgrade did not produce a complete V4 guard")
        return upgraded
    if OLDER_GUARD_MARKER in source:
        if source.count(OLDER_GUARD_MARKER) != 1 or source.count(_GUARD_V2) != 1:
            raise BridgeGuardError("bridge contains an unrecognized V2 guard")
        upgraded = source.replace(_GUARD_V2, _GUARD, 1)
        if not bridge_is_guarded(upgraded):
            raise BridgeGuardError("bridge V2 upgrade did not produce a complete V4 guard")
        return upgraded
    if LEGACY_GUARD_MARKER in source:
        if source.count(LEGACY_GUARD_MARKER) != 1 or _LEGACY_GUARD not in source:
            raise BridgeGuardError("bridge contains an unrecognized V1 guard")
        source = source.replace(_LEGACY_GUARD, "", 1)
    return source.replace(_EXPRESS_ANCHOR, _EXPRESS_ANCHOR + _GUARD, 1)


def _assert_regular_private_source(path: Path) -> os.stat_result:
    if not path.is_absolute():
        raise BridgeGuardError("bridge path must be absolute")
    _assert_no_symlink_components(path)
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise BridgeGuardError("bridge source does not exist") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise BridgeGuardError("bridge source must be a regular non-symlink file")
    if os.name == "posix" and details.st_mode & 0o022:
        raise BridgeGuardError("bridge source must not be group/world writable")
    return details


def check_bridge(path: Path) -> None:
    _assert_regular_private_source(path)
    source = path.read_text(encoding="utf-8")
    validate_compatible_source(source)
    if not bridge_is_guarded(source):
        raise BridgeGuardError("observe-only compatibility guard is not installed")


def apply_bridge_guard(path: Path, backup: Path) -> bool:
    details = _assert_regular_private_source(path)
    if not backup.is_absolute():
        raise BridgeGuardError("backup path must be absolute")
    _assert_no_symlink_components(backup, allow_missing_leaf=True)
    if backup.exists() or backup.is_symlink():
        raise BridgeGuardError("backup destination already exists")

    source = path.read_text(encoding="utf-8")
    patched = patch_bridge_source(source)
    if patched == source:
        return False

    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(source.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        backup.unlink(missing_ok=True)
        raise

    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".guarded", dir=path.parent
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(patched.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, stat.S_IMODE(details.st_mode))
        # The guard may run as root while the bridge service runs as a
        # dedicated account.  os.replace() otherwise makes the new bridge
        # root-owned and the next service start fails closed with EACCES.
        # Preserve the source owner on POSIX; Windows has no portable uid/gid
        # equivalent and keeps the existing ACL semantics.
        if os.name == "posix":
            os.chown(temporary_name, details.st_uid, details.st_gid)
        os.replace(temporary_name, path)
        temporary_name = None
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    check_bridge(path)
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser("check")
    check.add_argument("bridge", type=Path)
    apply = subcommands.add_parser("apply")
    apply.add_argument("bridge", type=Path)
    apply.add_argument("--backup", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check":
            check_bridge(args.bridge)
            print(f"BRIDGE_OBSERVE_ONLY_GUARD=PASS version={GUARD_VERSION}")
        else:
            changed = apply_bridge_guard(args.bridge, args.backup)
            state = "APPLIED" if changed else "ALREADY_PRESENT"
            print(f"BRIDGE_OBSERVE_ONLY_GUARD={state} version={GUARD_VERSION}")
    except (BridgeGuardError, OSError, UnicodeError) as exc:
        print(f"BRIDGE_OBSERVE_ONLY_GUARD=FAIL reason={type(exc).__name__}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
