# Document Library

Search your shared drives from AI agents, without copying the files out of the drive.

The service indexes documents where they already live (file shares, SharePoint, S3 and so on). It stores searchable text and vectors, plus a pointer back to each file. When an agent wants to read a document, the service fetches the live original.

**Key features**

- **Connect to almost any storage with a few lines of config.** Adapters are built on [rclone](https://rclone.org/overview/), so SMB shares, SharePoint, S3, SeaweedFS, SFTP, WebDAV, Google Drive and many more all work the same way. A new connection is a short declarative definition, in a config file or through one API call, with no code to write.
- **Swap in better models as they appear.** The text extractor, vision model and embedding model are all replaceable through configuration, and each can run on this machine, on a remote GPU or as a service you already have. When a better extraction model comes out, you can switch to it without rebuilding the system.
- **A simple web portal** for checking status, adding sources and setting where webhooks go.
- **Agents connect over MCP.** Agents get search and reading as MCP tools, and the same API is available over REST. If you choose, a trusted setup agent can also add sources over MCP, limited to locations you've approved.
- **Exact references.** Every result says which document and which page, slide, sheet or section it came from, and that reference opens the live file.
- **Semantic and keyword search together**, with results across languages.
- **Stays in sync.** New, changed, moved and deleted files are picked up automatically, and it can notify you through a webhook.
- **Handles scans.** Text extraction goes first, and pages it can't read well fall back to OCR and a vision model.
- **Runs anywhere.** It works without internet access or a GPU.

## Contents

- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [The web portal](#the-web-portal)
- [Using your own documents](#using-your-own-documents)
- [Connecting an agent over MCP](#connecting-an-agent-over-mcp)
- [What agents get back](#what-agents-get-back)
- [Adding sources with the connector API](#adding-sources-with-the-connector-api)
- [Letting a setup agent add sources](#letting-a-setup-agent-add-sources)
- [Models: run your own or use existing ones](#models-run-your-own-or-use-existing-ones)
- [Using an existing Postgres](#using-an-existing-postgres)
- [Change notifications](#change-notifications)
- [Logging and monitoring](#logging-and-monitoring)
- [Before production](#before-production)
- [Running the tests](#running-the-tests)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Current status and limits](#current-status-and-limits)

## Quick start

This runs everything on your machine, using the sample documents in `examples/sample-docs`. It takes about 10 minutes, most of it downloading.

**You need:**

- macOS or Linux, with `git` and `curl`
- [Docker](https://docs.docker.com/get-docker/) with Compose v2.24 or newer, and at least 6 GB of memory given to Docker. On Docker Desktop, that's under Settings > Resources.
- About 8 GB of free disk space

You don't need Python, Postgres or a GPU.

### 1. Get the code

```bash
git clone https://github.com/ys1173/document-library.git
```

```bash
cd document-library
```

### 2. Download the embedding model

This is about 2.2 GB. You only need to do it once.

```bash
scripts/fetch-models.sh
```

It finishes with `BAAI/bge-m3 is ready`.

### 3. Start the service

```bash
scripts/up.sh
```

The first run builds the images, which takes a few minutes. It then generates local passwords in `deploy/secrets` (ignored by git), and starts Postgres, the embedding model, the scanner, a worker and the APIs.

### 4. Check that the documents are indexed

```bash
curl -s http://localhost:8080/v1/list_sources | python3 -m json.tool
```

Indexing starts by itself and takes under a minute for the samples. It's finished when `files_by_status` shows `"indexed": 9`:

```json
{
    "sources": [
        {
            "source_id": "share",
            "root_path": "/data/share",
            "files_by_status": {
                "indexed": 9
            },
            "last_scan_ok": true
        }
    ]
}
```

If you get `Connection refused`, the service is still starting, so wait a few seconds and try again.

### 5. Search

```bash
curl -s http://localhost:8080/v1/search_docs -H 'Content-Type: application/json' -d '{"query": "how do I restart the SMF pods?", "top_k": 2}' | python3 -m json.tool
```

The top result comes from `Vendors/5G-Core-Ops-Guide.pdf`, with its page number. Look for the `citation` section, and copy the value of `uri`.

The search understands meaning across languages, so this Japanese question finds the same material:

```bash
curl -s http://localhost:8080/v1/search_docs -H 'Content-Type: application/json' -d '{"query": "SMFポッドを再起動するには?", "top_k": 2}' | python3 -m json.tool
```

### 6. Open the original

Replace `PASTE_URI_HERE` with the `uri` you copied. The service reads the file from the source and returns the cited page as Markdown:

```bash
curl -s http://localhost:8080/v1/open_doc -H 'Content-Type: application/json' -d '{"uri": "PASTE_URI_HERE"}' | python3 -m json.tool
```

### 7. See it pick up a new file

Add a document:

```bash
printf '# Maintenance\n\nThe xylophone procedure starts at midnight.\n' > examples/sample-docs/new-note.md
```

The scanner checks every 15 minutes by default. To skip the wait, ask it to scan now:

```bash
curl -s -X POST http://localhost:8081/v1/connectors/share/scan
```

Wait about 10 seconds, then search for it. `new-note.md` should be the top result:

```bash
curl -s http://localhost:8080/v1/search_docs -H 'Content-Type: application/json' -d '{"query": "xylophone procedure", "top_k": 1}' | python3 -m json.tool
```

Clean up the file afterwards. The next scan removes it from the index too:

```bash
rm examples/sample-docs/new-note.md
```

### 8. Stop

```bash
scripts/down.sh
```

Your index is kept for next time. To delete it as well, use `scripts/down.sh --delete-index`. That's always safe, because the index can be rebuilt from your documents.

## How it works

```
 Your documents (read-only)
 SMB, SharePoint, S3, SFTP, local...
          │
       rclone
          │
      ┌───▼─────┐  new, changed, moved, deleted     ┌───────────┐
      │ Scanner ├──────────────────────────────────►│ Job queue │
      └───┬─────┘                                   └─────┬─────┘
          │ webhook digest                                │
          ▼                                         ┌─────▼──────┐     ┌─────────────────┐
     Your receiver                                  │   Worker   ├────►│ Embedding model │
                                                    │ extract    │     └─────────────────┘
                                                    │ chunk      │
                                                    └─────┬──────┘
                                                          │
      ┌────────────────┐     ┌─────────────┐        ┌─────▼──────┐
      │  Vision model  │◄────┤ Vision      ├───────►│  Postgres  │
      │ (CPU or GPU)   │     │ worker      │        │  pgvector  │
      └────────────────┘     └─────────────┘        └─────┬──────┘
                                                          │
                       Agents ◄── REST + MCP ── API ──────┤
           Other services ──► Connector API ── Admin ─────┘
```

| Process | Command | What it does |
|---|---|---|
| Scanner | `doclib scanner` | Detects changes on the drive and sends webhook digests |
| Worker | `doclib worker` | Reads each changed file, extracts and chunks it, gets embeddings, and swaps it into the index atomically |
| Vision worker | `doclib vlm-worker` | Optional. Upgrades scanned pages in the background |
| API | `doclib api` | Read-only search and reading for agents, on port 8080 |
| Admin API | `doclib admin-api` | Web portal and connector management for other services, on port 8081 |

**Files aren't stored.** They're read into memory, processed and discarded. The database holds only the index, so you can delete it and rebuild it from the drive at any time.

**How a page is read:**

1. **Text extraction** runs first on every page. Pages it handles well stop here and are never sent to a vision model.
2. **OCR** runs straight away on pages that need more help, so the file is searchable immediately. Those are pages with little or no text, pages that are mostly images, pages with messy tables, and garbled pages. Image files always take this route.
3. **The vision model**, if enabled, then upgrades those pages in the background. It can't overwrite good text with something much shorter.

## The web portal

Open <http://localhost:8081> once the service is running.

| Page | What you can do |
|---|---|
| Status | See indexing progress, failures and each source's last scan, and try a search |
| Sources | Add a source, test its connection, scan now, or delete it |
| Webhook | Set the URL and signing secret, send a test, and see recent deliveries |
| Models | See which models are in use and whether they're reachable. This page is read-only |

**Login:** there's no login by default, because the portal is meant for an internal network. For a basic one, set a password when starting, and the browser will ask for it:

```bash
DOCLIB_PORTAL_PASSWORD=choose-a-password scripts/up.sh
```

The username is `admin`, or whatever you set in `DOCLIB_PORTAL_USERNAME`. With Helm, put these variables in the Secret named by `secrets.connectorEnv`.

The password and the admin API's tokens are separate. The password protects only the portal, and tokens protect only the REST API and admin MCP tools. So set both if people you don't trust can reach port 8081. To turn the portal off, set `admin.portal.enabled: false`.

**Credentials:** the portal only ever asks for the name of an environment variable, as described under [Credentials](#credentials). A new variable still has to be added to the service and needs a restart.

## Using your own documents

Point `DOCLIB_SHARE` at a folder and start again:

```bash
DOCLIB_SHARE=/path/to/your/documents scripts/up.sh
```

The folder is mounted read-only, and files must be readable by all users on your machine. Supported formats include PDF, Word, Excel, PowerPoint, HTML, Markdown, CSV, email, text, config files and images.

To change settings such as how often it scans or which files it includes, edit [deploy/config.compose.yaml](deploy/config.compose.yaml). [config.example.yaml](config.example.yaml) lists every option, and any option can be set by an environment variable: `DOCLIB_WORKER__CONCURRENCY=4` sets `worker.concurrency`.

## Connecting an agent over MCP

The service is an MCP server, using the streamable HTTP transport. Once it's running, the address is:

```
http://localhost:8080/mcp
```

Agents get five read-only tools: `search_docs`, `open_doc`, `get_doc_outline`, `list_sources` and `index_status`. They can't add sources or change anything.

**To connect**, add the address to your agent's MCP settings. Many clients use a format like this, but check your client's documentation for its exact format:

```json
{
  "mcpServers": {
    "document-library": {
      "type": "http",
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

In Claude Code, it's this command:

```bash
claude mcp add --transport http document-library http://localhost:8080/mcp
```

**To test it from Python**, install the client with `pip install "mcp<2"`, then run:

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client("http://localhost:8080/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print([tool.name for tool in tools.tools])
            result = await session.call_tool("search_docs", {"query": "how do I restart the SMF pods?", "top_k": 2})
            print(result.content[0].text)

asyncio.run(main())
```

**With auth turned on**, each client must send the token as a header: `Authorization: Bearer <token>`. See [Before production](#before-production).

## What agents get back

REST and MCP behave the same, and these are the tools on each:

| Tool | REST | Purpose |
|---|---|---|
| `search_docs` | `POST /v1/search_docs` | Find relevant text, with citations |
| `open_doc` | `POST /v1/open_doc` | Read part of the live original as Markdown |
| `get_doc_outline` | `GET /v1/get_doc_outline` | See a document's headings, to navigate long manuals |
| `list_sources` | `GET /v1/list_sources` | See sources, file counts and last scan times |
| `index_status` | `GET /v1/index_status` | See queue depth, failures and how fresh the index is |

`search_docs` can be filtered by `source_ids`, `path_prefix`, `tags`, `file_types`, `modified_after`, `language` and `block_types`.

Every search result says which document and where in it:

```json
{
  "text": "Restart SMF pods one at a time and wait for readiness before continuing ...",
  "score": 1.0,
  "citation": {
    "source_id": "share",
    "file_id": "f8d23c51-d84f-5700-93f8-f8c41aabd231",
    "path": "Vendors/5G-Core-Ops-Guide.pdf",
    "title": "5G Core Ops Guide",
    "anchor": "page:1",
    "heading_path": "5G Core Ops Guide > 4.2 Restarting SMF pods",
    "page_start": 1,
    "page_end": 1,
    "uri": "doclib://share/f8d23c51-d84f-5700-93f8-f8c41aabd231@sha256:2f67...#page:1"
  },
  "extraction_path": "fast",
  "quality": "ok"
}
```

| Format | Reference | Example |
|---|---|---|
| PDF | Page, or a page range for content that spans pages | `page:12`, `page:12-13` |
| PowerPoint | Slide | `slide:2` |
| Excel | Sheet name | `sheet:IP Plan` |
| Word, Markdown, HTML | Section, because these have no fixed pages | `heading:4.2` |

Agents have three ways to read a document:

- `open_doc` with `uri`, to open a citation or a saved reference
- `open_doc` with `file_id` and `page_range` (such as `"12-15"`), for specific pages
- `get_doc_outline`, to browse the headings first

**Good to know:**

- `file_id` stays the same when a file is renamed or moved, so saved references keep working.
- If the file changed since it was indexed, `open_doc` returns the live content, sets `stale_index: true` and queues a re-index.
- Documents can also be found by file name, folder or title, though a name match may rank below documents whose content matches.
- Page numbers are positions in the file, so PDF page 130 may be printed as "page 112".
- Document text is untrusted data and may contain malicious instructions. Responses carry `"content_is_untrusted": true`, so your agents must treat the text as data and never as commands.

## Adding sources with the connector API

Other services add document sources through the admin API on port 8081. Any backend that [rclone](https://rclone.org/overview/) supports works with no code changes, because the available types and options come from rclone's own catalogue.

This is how you connect a SeaweedFS bucket:

```bash
curl -X PUT http://localhost:8081/v1/connectors/seaweed-ops -H 'Content-Type: application/json' -d '{"type": "s3", "root_path": "team-docs/ops", "options": {"provider": "SeaweedFS", "endpoint": "http://seaweedfs:8333"}, "credentials": {"access_key_id": "DOCLIB_CRED_SEAWEED_ACCESS", "secret_access_key": "DOCLIB_CRED_SEAWEED_SECRET"}, "tags": ["type:ops"], "scan_interval": "15m"}'
```

The service checks it can read the source first (add `?test=false` to skip this). Scanning then starts on its own, with no restart.

This example needs a real SeaweedFS server, and the two environment variables described under [Credentials](#credentials). Until they're set, the call returns an error naming what's missing.

| Call | What it does |
|---|---|
| `GET /v1/connector-types` | Lists the connector types available |
| `GET /v1/connector-types/{type}` | Shows a type's options, and which are required or secret |
| `POST /v1/connectors/validate` | Checks a definition and its connection, storing nothing |
| `PUT /v1/connectors/{id}` | Creates or updates to this state. Safe to repeat: returns 201 if created, 200 if it already existed |
| `POST /v1/connectors` | Creates, with `id` in the body. Returns 409 if it already exists |
| `GET /v1/connectors` | Lists connectors with their status |
| `GET /v1/connectors/{id}` | Shows a connector's definition and status: files by status, last scan, last error |
| `POST /v1/connectors/{id}/scan` | Scans now, rather than at the next interval |
| `DELETE /v1/connectors/{id}` | Removes the connector and its index. Your source files aren't touched |

Every error has the same shape: `{"error": {"code": "...", "message": "...", "details": ...}}`. The full schema is at `http://localhost:8081/openapi.json`.

### Credentials

Credentials are environment variables, and connectors store only their names, never the values.

1. Name the variable with the `DOCLIB_CRED_` prefix. Others are refused, because otherwise a caller could request `DOCLIB_POSTGRES_PASSWORD` as a credential and send it to their own server.
2. Give it to every doclib process: scanner, worker, vlm-worker, api and admin. With Docker Compose, add it to `deploy/secrets/connectors.env`. With Helm, put it in the Secret named by `secrets.connectorEnv`.
3. Restart those processes, because a process can't see variables added after it starts.
4. Reference it in `credentials`, using the rclone option name. Secrets placed in `options` are refused.

### Which types are allowed

The default list is in `connectors.allowed_types`. `local` and wrapper backends such as `alias` and `crypt` are left out on purpose, because they could read this service's own files. SharePoint uses the `onedrive` type.

You can also declare sources in the config file, with the same fields under `sources:`. See [config.example.yaml](config.example.yaml).

## Letting a setup agent add sources

Ordinary agents can only search and read. You can also give one trusted agent separate MCP tools for adding sources, at `http://localhost:8081/mcp`. These tools are off by default.

They're narrower than the REST API above, because a document an agent reads could try to talk it into something. You approve each storage location in advance as a **connection profile**, which fixes its address and credentials. The agent can only choose a profile and a folder that profile allows.

| An agent can | An agent can't |
|---|---|
| See the approved profiles, and existing connectors with their status | Set an address, connector type or credentials |
| Check that a connector would work | Index a folder outside the profile's `allowed_roots` |
| Add a connector from a profile, with a folder, tags and file filters | Change or overwrite an existing connector |
| Start a scan | Delete anything. Use the REST API for that, or for changes |

**Everything an agent adds becomes searchable by every agent using the library**, so only list folders in `allowed_roots` that all of them may read.

### Turning it on

1. Add the credentials to `deploy/secrets/connectors.env`, as described under [Credentials](#credentials).

2. Define what agents may connect to in `deploy/config.compose.yaml`:

   ```yaml
   admin:
     mcp:
       profiles:
         team-bucket:
           description: Team documents in SeaweedFS
           type: s3
           options: {provider: SeaweedFS, endpoint: "http://seaweedfs:8333"}
           credentials: {access_key_id: DOCLIB_CRED_SEAWEED_ACCESS, secret_access_key: DOCLIB_CRED_SEAWEED_SECRET}
           allowed_roots: [team-docs/ops]   # This folder and those inside it. ["*"] allows the whole store.
           scan_interval: 15m
   ```

3. Start the service with the admin tools on:

   ```bash
   scripts/up.sh --admin-mcp
   ```

   This also turns on token auth for the whole admin API, so REST callers on port 8081 need a token from now on. The tools won't start without it.

4. Create a token for the agent. The subject appears in the logs next to everything the agent does:

   ```bash
   docker compose -p doclib exec -T admin doclib admin-token --subject setup-agent --days 30
   ```

5. Connect the agent, using a different server name from its search connection:

   ```bash
   claude mcp add --transport http document-library-admin http://localhost:8081/mcp --header "Authorization: Bearer <token>"
   ```

A profile change needs a restart of the admin service. So does a new credential, on every doclib process.

Outside Docker Compose, set `admin.mcp.enabled: true` and `admin.auth.mode: bearer` with a `token_secret_file`, and add `admin.mcp.profiles` to your config.

### Tools

| Tool | What it does |
|---|---|
| `list_connection_profiles` | Lists approved profiles and the folders each allows |
| `list_connectors` | Lists connectors with their definitions and status |
| `get_connector` | Shows one connector's definition and status |
| `validate_connector` | Checks a connector and that its folder is readable, storing nothing |
| `create_connector` | Adds a connector. Scanning starts straight away |
| `scan_connector` | Scans now, rather than at the next interval |

Refusals come back as `{"error": {"code": "...", "message": "...", "details": ...}}`. They're also logged as `admin.mcp_refused`, and additions are logged as `connector.applied` with the caller `mcp:<subject>`. Repeated refusals can be a sign of a misled agent, so they're worth an alert.

Keep the agent's token away from other agents, and keep port 8081 away from them too. There's no way yet to revoke one token on its own: to cancel tokens early, change the signing secret, which cancels them all. So keep expiry times short.

## Models: run your own or use existing ones

All models sit behind a URL, so each one can run on this machine, on another machine or in a service you already have.

### Embedding model

Any OpenAI-compatible `/v1/embeddings` service works. To use an existing one, set this in your config:

```yaml
embeddings:
  endpoint: https://embeddings.internal/v1
  model: BAAI/bge-m3
  dimension: 1024                      # must match the model
  # api_key_file: /run/secrets/embed_key
  # query_prefix: "query: "            # only for models that expect one, such as E5
  # document_prefix: "passage: "
```

Then leave out `deploy/compose.embed.yml` when you start the service. Run `doclib check` afterwards. It confirms that the service answers, and that its dimension matches your config and the database.

The model must support your languages. The bundled BGE-M3 covers English and Japanese among others, and it uses about 1.6 GB of memory.

**Changing models later** needs care, because vectors from different models can't be compared:

| Change | What to do |
|---|---|
| Same model, new server | Change the URL. Nothing else is needed |
| Different model, same dimension | Change the config, then run `doclib reindex --all`. Files stay searchable throughout, by keyword only until each one is redone |
| Different dimension | The column size is fixed at install, so this needs a new column and index. If your model isn't 1024 dimensions, set `embeddings.dimension` before the first `doclib migrate` |

### Vision model

The vision model is optional. Without it, hard pages stay on OCR. It's a standalone container with an OpenAI-compatible API, and only its URL changes between setups.

| Setup | Speed we measured | How |
|---|---|---|
| Local, on CPU | 30 to 170 seconds per page | `scripts/fetch-models.sh --vision`, then `scripts/up.sh --vision`. The model uses about 5 GB by itself, so allow at least 12 GB of Docker memory, including anything else you run in Docker |
| Another machine, with a GPU | About 3 seconds per page, on an Apple GPU | See below |
| A server you already run | Depends on your server | Set the URL. vLLM and other OpenAI-compatible servers should work |

**To use a GPU on another machine:** copy `deploy/vision` and the weights to that machine, create an API key, and start it:

```bash
mkdir -p secrets && openssl rand -hex 32 > secrets/vision_api_key
```

```bash
docker compose -f compose.gpu.yml up -d
```

Then set the service's config to use it, with the same key in a file. Use a higher `concurrency` on a GPU:

```yaml
extraction:
  vlm:
    enabled: true
    endpoint: https://gpu-host/v1
    api_key_file: /run/secrets/vision_api_key
    concurrency: 8
```

Page images cross the network in this setup, so put TLS or a private VPN in front of it.

Scanned files never wait for the vision model, because they're searchable on OCR text straight away. If the GPU is off, upgrade jobs simply wait, and `index_status` shows `vlm.reachable: false`.

### Text extractor

Text extraction is a plug-in chosen in config, so you can move to a better one later:

```yaml
extraction:
  text:
    backend: xberg                           # in-process (the default)
    # backend: http                          # a separate container, on any machine
    # endpoint: http://extract:8090/v1
    # backend: my_package.my_module:MyBackend  # your own class
```

- A new backend only has to return Markdown per page. The interface is in [base.py](src/doclib/extract/backends/base.py).
- `doclib extract-server` serves any in-process backend over HTTP, and shows the three routes a remote backend needs.
- To check a new backend, add it to `BACKENDS` in [test_backend_contract.py](tests/unit/test_backend_contract.py).
- After a swap, run `doclib reindex --all`. Each file records which extractor produced it, so you can see what's left. Page references survive a swap. Section references may change, and `open_doc` then returns `locator_found: false`.

## Using an existing Postgres

The service needs Postgres 16+ with pgvector 0.5 or newer (0.8+ recommended). Everything lives in its own `doclib` schema, and it doesn't need superuser after one setup step.

**1. A DBA runs this once** against the target database. It's safe to re-run, and touches nothing outside `doclib`:

```bash
psql "postgresql://dba@main-cluster:5432/platform" -v ON_ERROR_STOP=1 -v owner_password="$OWNER" -v worker_password="$WORKER" -v api_password="$API" -f deploy/postgres/existing-cluster.sql
```

**2. Give each process its own role**, through a connection string in a file:

| File | Role | Used by |
|---|---|---|
| `deploy/secrets/admin_dsn` | `doclib_owner` | `doclib migrate` only |
| `deploy/secrets/worker_dsn` | `doclib_worker` | Scanner, workers and admin API |
| `deploy/secrets/api_dsn` | `doclib_api` | Agent-facing API, which is read-only |

A connection string looks like `postgresql://doclib_worker:PASSWORD@main-cluster:5432/platform?sslmode=require`.

**3. Start without the bundled database**, by leaving out `deploy/compose.postgres.yml`:

```bash
docker compose -f deploy/docker-compose.yml -f deploy/compose.embed.yml up -d --build
```

If something is missing, `doclib migrate` stops and says what the DBA must run. Outside Compose, set `DOCLIB_POSTGRES__DSN_FILE=/run/secrets/dsn`.

Connect directly, or through PgBouncer in session mode. The scanner uses session advisory locks, and asyncpg's prepared statements don't suit transaction pooling.

## Change notifications

Every detected change is indexed automatically and reported through one generic webhook. Your receiver decides what to do with it, such as posting to Slack, sending email or opening a ticket.

The easiest way to set it up is the Webhook page of the [portal](#the-web-portal). Changes apply within seconds, with no restart. Settings saved there replace the ones in the config file. Changes made while the webhook is off are not sent later.

You can also set it in the config file:

```yaml
notifications:
  webhook:
    enabled: true
    url_file: /run/secrets/doclib_webhook_url
    secret_file: /run/secrets/doclib_webhook_secret   # optional signing
    digest_max_wait: 5m
```

The service sends one digest per source per scan, once indexing has finished:

```json
{
  "schema_version": "1.0",
  "event_id": "9b1c...",
  "event_type": "doclib.source.changes",
  "occurred_at": "2026-11-05T03:22:00Z",
  "tenant_id": "default",
  "source_id": "share",
  "part": 1,
  "parts": 1,
  "text": "share: 1 new, 1 changed, 1 failed",
  "summary": {"new": 1, "changed": 1, "deleted": 0, "moved": 0, "indexed": 1, "failed": 1, "skipped": 0},
  "changes": [
    {"change": "new", "outcome": "indexed", "file_id": "...", "path": "Vendors/Guide.pdf",
     "previous_path": null, "content_version": "...", "error": null}
  ]
}
```

- **Duplicates:** delivery is at-least-once, so receivers should ignore an `event_id` they've already seen.
- **Content:** messages carry paths and versions, never document text. File names do reach the receiver.
- **First index:** a source's first full scan sends nothing, so you aren't flooded.
- **Signatures:** if you set a secret, recompute `sha256=HMAC(secret, "{X-Doclib-Timestamp}.{raw body}")` and compare it with the `X-Doclib-Signature` header.

## Logging and monitoring

Every process writes structured JSON logs to stdout, covering scans, changes, job outcomes, routing, webhook deliveries and API requests. See them with:

```bash
docker compose -f deploy/docker-compose.yml -f deploy/compose.postgres.yml -f deploy/compose.embed.yml logs -f worker
```

- Set `logging.file` to also write a rotating file.
- A log filter removes document content, whatever the caller passes. Search queries are logged only as a length and a hash.
- Set `telemetry.otlp_endpoint` to also export logs, metrics and traces over OTLP.

## Before production

The quick start is for trying things out on your own machine. Before running this anywhere shared:

- [ ] **Turn on auth for the agent API.** Set `auth.mode` to `bearer` (signed tokens with a `tenant_id` claim) or `header` (behind a gateway using mTLS). The default is `none`.
- [ ] **Turn on auth for the admin API.** Set `admin.auth.mode` to `bearer`. Tokens also need the `doclib:admin` scope.
- [ ] **Keep the admin API away from ordinary agents.** Only other services, and a setup agent if you [use one](#letting-a-setup-agent-add-sources), should be able to reach port 8081.
- [ ] **Use read-only credentials for every source.** The service only ever reads, but the credential should enforce that too.
- [ ] **Decide about source permissions.** In this version, every agent can search everything indexed. Permissions are captured where available, but they aren't enforced.
- [ ] **Bundle the weights for air-gapped sites.** Build with `--build-arg BAKE_MODEL=BAAI/bge-m3`. Nothing is downloaded at runtime.
- [ ] **Check the licence report** for the images you ship: `docker run --rm -i --entrypoint python doclib:0.1.0 - < scripts/licence_report.py`

For Kubernetes, there's a Helm chart in [deploy/helm/doclib](deploy/helm/doclib).

## Running the tests

**All tests, with only Docker installed.** These run against real Postgres, rclone, tesseract, Samba and SeaweedFS, on a network with no internet access, and take about 2 minutes:

```bash
scripts/test-integration.sh
```

**Unit tests only**, which take seconds. These need Python 3.12 and [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.12 .venv
```

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
```

```bash
.venv/bin/python -m pytest tests/unit
```

**A full end-to-end run** on the real images, with a fake embedding model for speed. Fetch the vision model first, and allow at least 12 GB of Docker memory:

```bash
scripts/smoke.sh
```

## Project layout

```
src/doclib/          The service
  scanner.py         Change detection and scan loops
  worker.py          Indexing jobs
  vlm_worker.py      Background vision upgrades
  library.py         Search and reading, shared by REST and MCP
  api.py             Agent-facing API
  admin_api.py       Connector API
  admin_mcp.py       Optional connector tools for a setup agent
  portal.py          Web portal backend
  portal.html        Web portal page
  connectors.py      Connector validation, using rclone's catalogue
  rclone.py          Read-only source access
  notify.py          Webhook notifications
  chunker.py         Structure-aware chunking
  extract/           Text extraction, page routing and pluggable backends
  sql/               Database schema
deploy/              Dockerfiles, Compose files and the Helm chart
  vision/            Standalone vision model server
  embed/             Standalone embedding model server
docs/SPEC.md         Full requirements and design
examples/            Sample documents for the quick start
scripts/             Setup, start, stop and test scripts
tests/               Unit and integration tests
```

## Troubleshooting

**`curl: Connection refused`**
The service is still starting, or it failed to start. Check with `docker ps`. The services should all show as `Up`, and only `migrate` should have exited. Then look at the logs of whichever one isn't up.

**`files_by_status` shows `pending` for a long time**
The worker is probably waiting for the embedding model. Look for `job.retry_transient` in the worker logs. The worker retries every minute and doesn't count this against the file.

**`The embedding model isn't downloaded yet`**
Run `scripts/fetch-models.sh`.

**The vision container keeps restarting**
It's probably running out of memory. It needs about 6 GB for itself. Run `docker stats` to check, then give Docker more memory or run the model on another machine. Files stay searchable on OCR text in the meantime.

**A file shows as `failed`**
`GET /v1/index_status` lists failed jobs and their errors. Password-protected and corrupt files are the usual cause. Other files aren't affected.

**A scan says it `would delete` many files, and stops**
This is a safety check, so an unmounted share can't empty the index. If the deletes are real, run `doclib scan --allow-mass-delete`.

**Search results show `"degraded": ["vector"]`**
The embedding service can't be reached, so search is running on keywords only. Run `doclib check`.

## Current status and limits

The core service is built and tested, including connectors, notifications, the web portal, both MCP servers and the vision service. 289 tests pass, and the full stack passes its end-to-end checks with real models.

**Not yet tested on real systems:**

- Real-world documents. All testing so far used small generated files, so retrieval quality and the performance targets in the [spec](docs/SPEC.md) aren't measured yet.
- NVIDIA GPUs. GPU operation was tested on an Apple GPU only, and the NVIDIA image hasn't been built.
- SharePoint and OneDrive, against a real account.
- A real install of the Helm chart. It passes linting and rendering only.
- TLS connections to Postgres, and PgBouncer.
- Older and email formats: `.doc`, `.xls`, `.ppt`, `.eml` and `.msg`.

**Not built yet:**

- Keyword search for Japanese. Japanese documents are found through semantic search for now.
- A re-ranker, a faster re-embedding job, and secret redaction.
- Enforcing source permissions on search results.
- A benchmark harness for comparing extractors and models on real documents.

**Known limits:**

- Excel references stop at the sheet, with no row numbers.
- Keyword search has no stop-word list, so very common words can add noise to ranking.
- The vision model can lose indentation in code blocks, which is one reason text extraction always goes first.
