// Minimal executable facsimile of the bridge contracts used by bridge_guard.
// It intentionally has no third-party dependencies, socket or network access.
const routes = [];
const fakeApp = {
  use(handler) { routes.push({ method: null, path: null, handler }); },
  get(routePath, handler) { routes.push({ method: 'GET', path: routePath, handler }); },
  post(routePath, handler) { routes.push({ method: 'POST', path: routePath, handler }); },
  async dispatch(input) {
    const requestPath = input.path;
    const req = {
      method: input.method || 'POST',
      path: requestPath,
      body: input.body || {},
      headers: { host: '127.0.0.1:3011', ...(input.headers || {}) },
      socket: { remoteAddress: input.remoteAddress || '127.0.0.1' },
      get(name) { return this.headers[String(name).toLowerCase()]; },
    };
    const result = { status: 200, body: null, sends };
    const res = {
      status(value) { result.status = value; return this; },
      json(value) { result.body = value; return result; },
    };
    async function visit(index) {
      if (index >= routes.length) return res.status(404).json({ error: 'not_found' });
      const layer = routes[index];
      if (layer.method && (layer.method !== req.method || layer.path !== req.path)) {
        return visit(index + 1);
      }
      let nextCalled = false;
      const next = async () => { nextCalled = true; return visit(index + 1); };
      const returned = await layer.handler(req, res, next);
      if (nextCalled) return returned;
      return returned === undefined ? result : returned;
    }
    await visit(0);
    return result;
  },
};
const express = () => fakeApp;
express.json = () => (_req, _res, next) => next();

const OBSERVE_ONLY = true;
const sock = {};
const connectionState = process.env.ESPELHO_FIXTURE_CONNECTION_STATE || 'connected';
const sends = [];
let sentSequence = 0;
const messageStore = { remember() {} };

function buildTextSendPayload(message) {
  return { content: { text: message }, options: {} };
}

function sendManualWithTimeout(chatId, payload, options = {}) {
  sends.push({ chatId, payload: summarizePayload(payload), options });
  sentSequence += 1;
  return Promise.resolve({ key: { id: `sent-${sentSequence}` } });
}

function trackSentMessageId(_message) {}

function summarizePayload(payload) {
  const result = { ...payload };
  for (const key of ['image', 'audio', 'video', 'document']) {
    if (Buffer.isBuffer(result[key])) result[key] = `buffer:${result[key].length}`;
  }
  return result;
}

const app = express();
app.use(express.json());

app.post('/mirror-manual-send', async (_req, res) => {
  return res.json({ success: true, legacy: true });
});

app.post('/send', async (_req, res) => {
  return res.json({ success: true, generic: true });
});

app.post('/send-media', async (_req, res) => {
  return res.json({ success: true, genericMedia: true });
});

if (process.env.ESPELHO_FIXTURE_REQUEST) {
  app.dispatch(JSON.parse(process.env.ESPELHO_FIXTURE_REQUEST))
    .then(result => process.stdout.write(`${JSON.stringify(result)}\n`))
    .catch(error => {
      process.stderr.write(`${error.stack || error}\n`);
      process.exitCode = 1;
    });
}
