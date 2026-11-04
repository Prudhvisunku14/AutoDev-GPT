# n8n workflows for DevPilot

DevPilot triggers only the workflows registered in [`config/n8n_workflows.yaml`](../config/n8n_workflows.yaml). Every trigger is signed, waits for approval unless `N8N_AUTO_APPROVE=true`, and posts to `<N8N_BASE_URL>/webhook/<path>`. Workflows that report back post a signed result to `POST /integrations/n8n/callback`. Design: [ARCHITECTURE.md §8.1](../docs/ARCHITECTURE.md).

**Status:** the adapter, the callback and the health check are tested offline against a simulated n8n. The templates' signing code has been run in Node.js and agrees with DevPilot byte for byte. **Neither template has been imported into a real n8n instance yet.**

| Template | Key | What it does |
|---|---|---|
| `workflows/devpilot-echo.json` | `echo` | Verifies the signature, then posts the text back, signed. Use it to check both directions. |
| `workflows/devpilot-notify.json` | `notify` | Verifies the signature and passes on `title` and `message`. Replace its last node with your channel (Slack, email, …). |

## Trying it with a local n8n

1. Start n8n on loopback only. The secret must be the same value as DevPilot's `N8N_WEBHOOK_SECRET`:

   ```powershell
   docker run -d --name devpilot-n8n -p 127.0.0.1:5678:5678 `
     -e NODE_FUNCTION_ALLOW_BUILTIN=crypto -e N8N_BLOCK_ENV_ACCESS_IN_NODE=false `
     -e DEVPILOT_WEBHOOK_SECRET=<the same secret> n8nio/n8n
   ```

2. Open `http://127.0.0.1:5678`, import both templates, and activate them. Create an API key under *Settings → n8n API*.
3. Set the following in DevPilot's `.env`:
   - `N8N_ENABLED=true`
   - `N8N_WEBHOOK_SECRET`
   - `N8N_API_KEY`
   - `N8N_CALLBACK_URL=http://host.docker.internal:8000/integrations/n8n/callback`, which is how the n8n container reaches DevPilot on Docker Desktop.
4. Start DevPilot and call `GET /api/v1/integrations/n8n/status` with your API token. Both workflows should show `found: true, active: true`, and the response should have `ready: true`.

`127.0.0.1:5678` is already the n8n entry in `config/egress_allowlist.yaml`. Any other address is refused until it is listed there.
