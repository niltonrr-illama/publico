from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class OpenClawHumanOutboundTest(unittest.TestCase):
    def test_mapped_human_topic_sends_text_and_media_without_agent(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "plugin.mjs"
            plugin.write_text(
                (PROJECT / "integrations" / "openclaw" / "dist" / "index.js").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            sdk = root / "node_modules" / "openclaw" / "plugin-sdk"
            sdk.mkdir(parents=True)
            (sdk.parent / "package.json").write_text(
                json.dumps(
                    {
                        "name": "openclaw",
                        "type": "module",
                        "exports": {
                            "./plugin-sdk/plugin-entry": "./plugin-sdk/plugin-entry.js"
                        },
                    }
                ),
                encoding="utf-8",
            )
            (sdk / "plugin-entry.js").write_text(
                "export const definePluginEntry = (value) => value;\n",
                encoding="utf-8",
            )
            runner = root / "runner.mjs"
            runner.write_text(
                textwrap.dedent(
                    r"""
                    import assert from "node:assert/strict";
                    import { existsSync, mkdirSync, readFileSync, truncateSync, writeFileSync } from "node:fs";
                    import path from "node:path";

                    const root = process.cwd();
                    const mediaRoot = path.join(root, "source-media");
                    const managedRoot = path.join(root, "managed-media");
                    mkdirSync(mediaRoot, { recursive: true });
                    mkdirSync(managedRoot, { recursive: true });
                    const mediaFiles = [
                      ["photo.jpg", "image", "image/jpeg"],
                      ["file_0.oga", "voice", "audio/ogg"],
                      ["audio.mp3", "audio", "audio/mpeg"],
                      ["video.mp4", "video", "video/mp4"],
                      ["document.pdf", "document", "application/pdf"],
                    ].map(([name, kind, contentType]) => {
                      const filePath = path.join(mediaRoot, name);
                      writeFileSync(filePath, Buffer.from(`${kind}-bytes`));
                      return { path: filePath, kind, contentType, fileName: name };
                    });
                    const routeMap = path.join(root, "route-map.json");
                    writeFileSync(routeMap, JSON.stringify({
                      forum_chat_id: "-10099",
                      routes: { "15550000001": { topicId: "77", enabled: true } },
                    }));
                    const config = path.join(root, "config.toml");
                    const cli = path.join(root, "espelho-zap");
                    writeFileSync(config, "");
                    writeFileSync(cli, "");
                    Object.assign(process.env, {
                      ESPELHO_ZAP_CLI: cli,
                      ESPELHO_ZAP_CONFIG: config,
                      ESPELHO_ZAP_SOURCE_PROFILE_ID: "openclaw-test",
                      ESPELHO_ZAP_PRIVACY_SCOPE: "owner_private",
                      ESPELHO_ZAP_HOOK_HEALTH_FILE: path.join(root, "health.json"),
                      ESPELHO_ZAP_MEDIA_ROOTS: mediaRoot,
                      ESPELHO_ZAP_TELEGRAM_FORUM_CHAT_ID: "-10099",
                      ESPELHO_ZAP_HUMAN_OUTBOUND_ENABLED: "enabled",
                      ESPELHO_ZAP_HUMAN_OUTBOUND_ALLOWED_USERS: "42",
                      ESPELHO_ZAP_HUMAN_OUTBOUND_ROUTE_MAP: routeMap,
                      ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER: path.join(root, "outbound.jsonl"),
                      ESPELHO_ZAP_HUMAN_OUTBOUND_MANAGED_MEDIA_ROOT: managedRoot,
                      ESPELHO_ZAP_HUMAN_OUTBOUND_WHATSAPP_ACCOUNT_ID: "paired-account",
                    });

                    const sends = [];
                    async function waitFor(predicate, label) {
                      const deadline = Date.now() + 2000;
                      while (!predicate()) {
                        if (Date.now() >= deadline) throw new Error(`timeout:${label}`);
                        await new Promise((resolve) => setTimeout(resolve, 5));
                      }
                    }
                    let mediaCallCount = 0;
                    let failAtMediaCall = 0;
                    const adapter = {
                      textChunkLimit: 4000,
                      chunker(text, limit) {
                        return text.length > limit ? [text.slice(0, limit), text.slice(limit)] : [text];
                      },
                      async sendText(value) {
                        sends.push({ kind: "text", ...value });
                        return { channel: "whatsapp", messageId: `wa-${sends.length}` };
                      },
                      async sendMedia(value) {
                        assert.equal(existsSync(value.mediaUrl), true, "staged media must exist during send");
                        mediaCallCount += 1;
                        sends.push({ kind: "media", ...value });
                        if (mediaCallCount === failAtMediaCall) throw new Error("platform_send_uncertain");
                        return { channel: "whatsapp", messageId: `wa-${sends.length}` };
                      },
                    };
                      const handlers = new Map();
                      const plugin = (await import("./plugin.mjs")).default;
                      plugin.register({
                        config: { channels: { whatsapp: { pluginHooks: { messageReceived: true } } } },
                        runtime: { channel: { outbound: {
                          async loadAdapter(channel) {
                            assert.equal(channel, "whatsapp");
                            return adapter;
                          },
                        } } },
                        on(name, handler) { handlers.set(name, handler); },
                      });
                      const received = handlers.get("message_received");
                      const beforeReply = handlers.get("before_agent_reply");
                      const sending = handlers.get("message_sending");

                      const baseMetadata = {
                        to: "telegram:-10099",
                        originatingTo: "telegram:-10099:topic:77",
                      };
                      const ctx = {
                        channelId: "telegram",
                        conversationId: "telegram:-10099:topic:77",
                        senderId: "42",
                      };
                      const textEvent = {
                        from: "telegram:user:42", content: "texto humano", threadId: "77",
                        messageId: "1001", senderId: "42", metadata: baseMetadata,
                      };
                      await received(textEvent, ctx);
                      await waitFor(() => sends.length === 1, "text send");
                      assert.equal(sends.length, 1);
                      assert.equal(sends[0].kind, "text");
                      assert.equal(sends[0].to, "15550000001@s.whatsapp.net");
                      assert.equal(sends[0].text, "texto humano");
                      assert.equal(sends[0].accountId, "paired-account");

                      await received(textEvent, ctx);
                      assert.equal(sends.length, 1, "Telegram message id must dedupe");
                      await received({ ...textEvent, content: "payload alterado" }, ctx);
                      assert.equal(sends.length, 1, "same Telegram id with changed payload must conflict");

                      await received({ ...textEvent, messageId: "1002", senderId: "99" }, { ...ctx, senderId: "99" });
                      await received({ ...textEvent, messageId: "1003", isBot: true }, ctx);
                      await received({ ...textEvent, messageId: "1003a", messageType: "service" }, ctx);
                      await received({ ...textEvent, messageId: "1003b", metadata: { ...baseMetadata, event_type: "service_message" } }, ctx);
                      await received({ ...textEvent, messageId: "1003c", raw_message: { is_automation: true } }, ctx);
                      await received({ ...textEvent, messageId: "1003d" }, { ...ctx, isAutomatic: true });
                      await received({ ...textEvent, messageId: "1003e", rawMessage: { serviceMessage: {} } }, ctx);
                      assert.equal(sends.length, 1, "non-owner and bot must never send");

                      const mediaEvent = {
                        from: "telegram:user:42", content: "legenda humana", threadId: "77",
                        messageId: "1004", senderId: "42",
                        metadata: {
                          ...baseMetadata,
                          mediaPaths: mediaFiles.map((item) => item.path),
                          mediaTypes: mediaFiles.map((item) => item.contentType),
                        },
                      };
                      await received({
                        ...mediaEvent,
                        messageId: "1003z",
                        metadata: { ...mediaEvent.metadata, mediaStagingPending: true },
                      }, ctx);
                      assert.equal(sends.length, 1, "early unstaged media fact must wait");
                      await received(mediaEvent, ctx);
                      await waitFor(() => sends.length === 6, "media send");
                      assert.equal(sends.length, 6);
                      assert.deepEqual(
                        sends.slice(1).map((item) => path.extname(item.mediaUrl)),
                        [".jpg", ".oga", ".mp3", ".mp4", ".pdf"],
                      );
                      assert.equal(sends[1].text, "legenda humana");
                      assert.deepEqual(sends.slice(2).map((item) => item.text), ["", "", "", ""]);
                      assert.equal(sends[2].audioAsVoice, true);
                      assert.equal(sends[3].audioAsVoice, false);
                      assert.equal(sends[5].forceDocument, true);
                      assert.equal(sends[5].fileName, "document.pdf");
                      assert.equal(path.basename(sends[5].mediaUrl), "document.pdf");
                      assert.deepEqual(sends[1].mediaLocalRoots, [managedRoot]);
                      assert.equal(
                        sends.slice(1).every((item) => !existsSync(item.mediaUrl)),
                        true,
                        "confirmed media must retire",
                      );

                      failAtMediaCall = mediaCallCount + 2;
                      await received({
                        ...mediaEvent,
                        messageId: "1005",
                        metadata: {
                          ...baseMetadata,
                          mediaPaths: mediaFiles.slice(0, 2).map((item) => item.path),
                          mediaTypes: mediaFiles.slice(0, 2).map((item) => item.contentType),
                        },
                      }, ctx);
                      await waitFor(() => sends.length === 8, "uncertain media send");
                      const retained = sends.slice(-2).map((item) => item.mediaUrl);
                      assert.equal(retained.every((item) => existsSync(item)), true);

                      const gateMedia = path.join(mediaRoot, "gate-large.jpg");
                      writeFileSync(gateMedia, "");
                      truncateSync(gateMedia, 64 * 1024 * 1024);
                      const gateSessionKey = "agent:main:telegram:group:-10099:topic:77";
                      const gateEvent = {
                        ...mediaEvent,
                        messageId: "1005-gate",
                        metadata: {
                          ...baseMetadata,
                          mediaPaths: [gateMedia],
                          mediaTypes: ["image/jpeg"],
                        },
                      };
                      const gateReceived = received(gateEvent, { ...ctx, sessionKey: gateSessionKey });
                      const gatePromise = beforeReply(
                        { cleanedBody: "legenda humana" },
                        {
                          channel: "telegram",
                          chatId: "telegram:-10099:topic:77",
                          channelId: "telegram:-10099:topic:77",
                          sessionKey: gateSessionKey,
                        },
                      );
                      assert.equal(typeof gatePromise?.then, "function", "pre-model gate must await capture");
                      let gateSettled = false;
                      gatePromise.then(() => { gateSettled = true; });
                      await new Promise((resolve) => setImmediate(resolve));
                      assert.equal(gateSettled, false, "large media reservation must hold the pre-model gate");
                      await gateReceived;
                      assert.deepEqual(
                        await gatePromise,
                        { handled: true, reason: "espelho-zap-forum-data-plane" },
                      );
                      await waitFor(() => sends.length === 9, "gated media send");
                      const sendCountAfterGate = sends.length;

                      const oversized = path.join(mediaRoot, "oversized.bin");
                      writeFileSync(oversized, "");
                      truncateSync(oversized, (128 * 1024 * 1024) + 1);
                      await received({
                        ...mediaEvent,
                        messageId: "1006",
                        content: "",
                        metadata: {
                          ...baseMetadata,
                          mediaPaths: [oversized],
                          mediaTypes: ["application/octet-stream"],
                        },
                      }, ctx);
                      assert.equal(sends.length, sendCountAfterGate, "oversized item must fail before copy/send");

                      const aggregateMedia = [
                        ["aggregate-a.bin", 128 * 1024 * 1024],
                        ["aggregate-b.bin", 128 * 1024 * 1024],
                        ["aggregate-c.bin", 1],
                      ].map(([name, size]) => {
                        const filePath = path.join(mediaRoot, name);
                        writeFileSync(filePath, "");
                        truncateSync(filePath, size);
                        return { path: filePath, kind: "document", contentType: "application/octet-stream" };
                      });
                      await received({
                        ...mediaEvent,
                        messageId: "1007",
                        content: "",
                        metadata: {
                          ...baseMetadata,
                          mediaPaths: aggregateMedia.map((item) => item.path),
                          mediaTypes: aggregateMedia.map((item) => item.contentType),
                        },
                      }, ctx);
                      assert.equal(sends.length, sendCountAfterGate, "oversized aggregate must fail before copy/send");

                      assert.deepEqual(
                        await beforeReply(textEvent, ctx),
                        { handled: true, reason: "espelho-zap-forum-data-plane" },
                      );
                      assert.equal(await beforeReply(
                        { ...textEvent, metadata: { to: "telegram:-10000", originatingTo: "telegram:-10000:topic:77" } },
                        { ...ctx, conversationId: "telegram:-10000:topic:77" },
                      ), undefined);
                      assert.deepEqual(
                        sending({ platform: "whatsapp" }, {}),
                        { cancel: true, cancelReason: "espelho-zap-passive" },
                      );

                      const ledger = readFileSync(path.join(root, "outbound.jsonl"), "utf8");
                      assert.match(ledger, /"status":"sent"/);
                      assert.match(ledger, /"status":"uncertain"/);
                      assert.match(ledger, /"remoteMessageIds":\["wa-7"\]/);
                      assert.doesNotMatch(ledger, /telegram:-10099:77:1006/);
                      assert.doesNotMatch(ledger, /telegram:-10099:77:1007/);
                      assert.doesNotMatch(ledger, /99@s\.whatsapp\.net/);

                      let slowCalls = 0;
                      adapter.sendText = async () => {
                        slowCalls += 1;
                        return new Promise(() => {});
                      };
                      const slowResult = await Promise.race([
                        received({ ...textEvent, messageId: "1008", content: "envio lento" }, ctx).then(() => "returned"),
                        new Promise((resolve) => setTimeout(() => resolve("timeout"), 100)),
                      ]);
                      assert.equal(slowResult, "returned", "hook must return after durable reservation");
                      await new Promise((resolve) => setImmediate(resolve));
                      const queuedResult = await Promise.race([
                        received({ ...textEvent, messageId: "1009", content: "segundo WIP" }, ctx).then(() => "returned"),
                        new Promise((resolve) => setTimeout(() => resolve("timeout"), 100)),
                      ]);
                      assert.equal(queuedResult, "returned");
                      await new Promise((resolve) => setImmediate(resolve));
                      assert.equal(slowCalls, 1, "WIP=1 must not start the second send");
                      const slowLedger = readFileSync(path.join(root, "outbound.jsonl"), "utf8");
                      assert.match(slowLedger, /telegram:-10099:77:1008/);
                      assert.match(slowLedger, /telegram:-10099:77:1009/);
                      assert.match(slowLedger, /"status":"dispatching"/);
                      assert.match(slowLedger, /"status":"reserved"/);
                      const health = JSON.parse(readFileSync(path.join(root, "health.json"), "utf8"));
                      assert.equal(health.failures.outbound_replay_conflict, 1);
                    """
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(runner)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)

    def test_ledger_recovers_partial_tail_compacts_and_rejects_earlier_corruption(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plugin.mjs").write_text(
                (PROJECT / "integrations" / "openclaw" / "dist" / "index.js").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            sdk = root / "node_modules" / "openclaw" / "plugin-sdk"
            sdk.mkdir(parents=True)
            (sdk.parent / "package.json").write_text(
                json.dumps(
                    {
                        "name": "openclaw",
                        "type": "module",
                        "exports": {
                            "./plugin-sdk/plugin-entry": "./plugin-sdk/plugin-entry.js"
                        },
                    }
                ),
                encoding="utf-8",
            )
            (sdk / "plugin-entry.js").write_text(
                "export const definePluginEntry = (value) => value;\n",
                encoding="utf-8",
            )
            runner = root / "runner.mjs"
            runner.write_text(
                textwrap.dedent(
                    r"""
                    import assert from "node:assert/strict";
                    import {
                      mkdirSync, readFileSync, readdirSync, statSync, symlinkSync,
                      truncateSync, writeFileSync,
                    } from "node:fs";
                    import path from "node:path";

                    const root = process.cwd();
                    const routeMap = path.join(root, "route-map.json");
                    const managedRoot = path.join(root, "managed-media");
                    mkdirSync(managedRoot, { recursive: true });
                    writeFileSync(routeMap, JSON.stringify({
                      forum_chat_id: "-10099",
                      routes: { "15550000001": { topicId: "77", enabled: true } },
                    }));
                    Object.assign(process.env, {
                      ESPELHO_ZAP_CLI: path.join(root, "espelho-zap"),
                      ESPELHO_ZAP_CONFIG: path.join(root, "config.toml"),
                      ESPELHO_ZAP_SOURCE_PROFILE_ID: "openclaw-test",
                      ESPELHO_ZAP_PRIVACY_SCOPE: "owner_private",
                      ESPELHO_ZAP_HOOK_HEALTH_FILE: path.join(root, "health.json"),
                      ESPELHO_ZAP_TELEGRAM_FORUM_CHAT_ID: "-10099",
                      ESPELHO_ZAP_HUMAN_OUTBOUND_ENABLED: "enabled",
                      ESPELHO_ZAP_HUMAN_OUTBOUND_ALLOWED_USERS: "42",
                      ESPELHO_ZAP_HUMAN_OUTBOUND_ROUTE_MAP: routeMap,
                      ESPELHO_ZAP_HUMAN_OUTBOUND_MANAGED_MEDIA_ROOT: managedRoot,
                    });
                    writeFileSync(process.env.ESPELHO_ZAP_CLI, "");
                    writeFileSync(process.env.ESPELHO_ZAP_CONFIG, "");

                    const ledger = path.join(root, "outbound.jsonl");
                    const repeated = JSON.stringify({
                      requestId: "telegram:-10099:77:1",
                      status: "sent",
                      updatedAt: "2026-08-04T00:00:00Z",
                      padding: "x".repeat(2048),
                    }) + "\n";
                    writeFileSync(ledger, repeated.repeat(5000) + '{"requestId":');
                    assert.ok(statSync(ledger).size > 8 * 1024 * 1024);
                    process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER = ledger;

                    const plugin = (await import("./plugin.mjs")).default;
                    const api = {
                      config: { channels: { whatsapp: { pluginHooks: { messageReceived: true } } } },
                      on() {},
                    };
                    plugin.register(api);
                    if (process.platform !== "win32") {
                      assert.equal(statSync(root).mode & 0o777, 0o700);
                      assert.equal(statSync(managedRoot).mode & 0o777, 0o700);
                      assert.equal(statSync(ledger).mode & 0o777, 0o600);
                    }
                    const compacted = readFileSync(ledger, "utf8");
                    const compactLines = compacted.trim().split("\n");
                    assert.equal(compactLines.length, 1);
                    assert.equal(JSON.parse(compactLines[0]).status, "sent");
                    assert.ok(statSync(ledger).size < 4096);
                    const backups = readdirSync(root).filter((name) => name.startsWith("outbound.jsonl.truncated-") && name.endsWith(".bak"));
                    assert.equal(backups.length, 1);
                    assert.ok(statSync(path.join(root, backups[0])).size > 8 * 1024 * 1024);

                    const badLedger = path.join(root, "bad.jsonl");
                    writeFileSync(badLedger, repeated + "not-json\n" + repeated);
                    process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER = badLedger;
                    assert.throws(() => plugin.register(api), /human_outbound_ledger_invalid/);

                    const tooLarge = path.join(root, "too-large.jsonl");
                    writeFileSync(tooLarge, "");
                    truncateSync(tooLarge, (128 * 1024 * 1024) + 1);
                    process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER = tooLarge;
                    assert.throws(() => plugin.register(api), /human_outbound_ledger_too_large/);

                    const directoryLedger = path.join(root, "directory-ledger");
                    mkdirSync(directoryLedger);
                    process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER = directoryLedger;
                    assert.throws(() => plugin.register(api), /human_outbound_ledger_invalid/);

                    const ledgerTarget = path.join(root, "ledger-target.jsonl");
                    const ledgerLink = path.join(root, "ledger-link.jsonl");
                    writeFileSync(ledgerTarget, "");
                    try {
                      symlinkSync(ledgerTarget, ledgerLink, "file");
                      process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER = ledgerLink;
                      assert.throws(() => plugin.register(api), /human_outbound_ledger_invalid/);
                    } catch (error) {
                      if (!(error && typeof error === "object" && ["EPERM", "EACCES", "UNKNOWN"].includes(error.code))) {
                        throw error;
                      }
                    }

                    const realParent = path.join(root, "real-ledger-parent");
                    const linkedParent = path.join(root, "linked-ledger-parent");
                    mkdirSync(realParent);
                    try {
                      symlinkSync(realParent, linkedParent, process.platform === "win32" ? "junction" : "dir");
                      process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER = path.join(linkedParent, "outbound.jsonl");
                      assert.throws(() => plugin.register(api), /human_outbound_ledger_invalid_parent/);
                    } catch (error) {
                      if (!(error && typeof error === "object" && ["EPERM", "EACCES", "UNKNOWN"].includes(error.code))) {
                        throw error;
                      }
                    }

                    const realManaged = path.join(root, "real-managed");
                    const linkedManaged = path.join(root, "linked-managed");
                    mkdirSync(realManaged);
                    try {
                      symlinkSync(realManaged, linkedManaged, process.platform === "win32" ? "junction" : "dir");
                      process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER = path.join(root, "fresh-ledger.jsonl");
                      process.env.ESPELHO_ZAP_HUMAN_OUTBOUND_MANAGED_MEDIA_ROOT = linkedManaged;
                      assert.throws(() => plugin.register(api), /human_outbound_managed_media_root_required/);
                    } catch (error) {
                      if (!(error && typeof error === "object" && ["EPERM", "EACCES", "UNKNOWN"].includes(error.code))) {
                        throw error;
                      }
                    }
                    """
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(runner)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
