# tripo-mcp

An MCP server that lets an AI assistant make 3D models and images with [Tripo](https://www.tripo3d.ai): text or image to model, four views to model, rigging, preset animations, format conversion and image generation, with the results downloaded to disk.

One Python file, standard library only, spoken over stdio. Requires Python 3.9 or newer.

## Pick a backend

Tripo sells two separate things, and each has its own credits. The server can use either, and you choose per call.

| | `api` | `cli` |
|---|---|---|
| What it talks to | Tripo's documented v3 REST API | the Go [tripo-cli](https://github.com/vast-enterprise/tripo-cli), signed in as your Tripo Studio account |
| Credits spent | API credits (pay as you go, 1 credit = 0.01 USD) | Studio credits (your Studio plan) |
| Sign-in | an API key | `tripo-cli auth login` (a code is emailed to you) |
| Only here | `tripo_upload` | four views to a model, `tripo_image`, credit expiry in every result |

Studio credits cannot pay for API calls and API credits cannot pay for the CLI. If you have a Studio plan and no API credits, use `cli`.

Choose with the `backend` argument on a tool call, or set `TRIPO_BACKEND=api` or `cli`. With neither, the server uses `api` when a key exists and `cli` otherwise.

## Setup

1. Register the server (use `--scope project` to keep it to one repo):

   ```sh
   claude mcp add tripo --scope user -- python3 /path/to/tripo-mcp.py
   ```

2. Set up at least one backend.

   **api:** create a key at https://platform.tripo3d.ai (API Keys, then "Create Key"; it is shown once). Then either call the `tripo_set_key` tool, or export `TRIPO_API_KEY`. The tool writes `tripo/key` under `$XDG_CONFIG_HOME`, or `%APPDATA%` on Windows, or `~/.config` otherwise, with mode 600 where the OS honors it. `TRIPO_API_KEY` wins over the file.

   **cli:**

   ```sh
   go install github.com/vast-enterprise/tripo-cli@v0.6.0
   tripo-cli auth login
   ```

   The server finds the binary on `PATH`, in `GOBIN` or `GOPATH/bin` (default `~/go/bin`), or at `TRIPO_CLI`. The CLI renews its own session. When it expires, the error says to run `tripo-cli auth login` again.

3. Restart the assistant so it loads the new tools.

Environment variables: `TRIPO_BACKEND`, `TRIPO_API_KEY`, `TRIPO_CLI`, and `TRIPO_API_BASE` to override the API URL. Set them in the `env` block of the MCP server entry if you want them to apply to the server.

## Tools

| Tool | What it does | Backend |
|---|---|---|
| `tripo_generate` | Text, one image, or four views (front, back, left, right) to a textured 3D model | both; four views cli only |
| `tripo_image` | Generate an image from a prompt, optionally from an input image or a sketch | cli only |
| `tripo_rig` | Add a skeleton | both |
| `tripo_retarget` | Apply preset animations to a rigged model | both |
| `tripo_convert` | Convert to another format | both |
| `tripo_task` | api: wait for an earlier task and save its model. cli: show its details | both |
| `tripo_balance` | Show credits. On the cli: how many expire, when, and the plan | both |
| `tripo_upload` | Upload a local image or model, returns a file token | api only |
| `tripo_set_key` | Save the API key | api only |

Generation costs credits. `tripo_image` is free within Studio's monthly image allowance. Check `tripo_balance` before and after if you are unsure.

### Typical flow

1. `tripo_generate` with an image or prompt and `out` set to a file or directory. It waits, downloads the model, and prints the task id (and on the cli, the project id).
2. `tripo_rig` with that id as `input`.
3. `tripo_retarget` with the rig result's id and the clip names.
4. `tripo_convert` if you need another format.

### What differs between backends

- **Inputs.** On `api` an input is a task id (`task_...`), a file token (`file_...`), a URL, or a local path, which is uploaded for you. On `cli` it is the project id that `tripo_generate` printed, and images must be local files.
- **Animation names.** `api` uses `preset:walk`, `cli` uses `preset:biped:walk`.
- **Unsupported options.** An option a backend lacks is rejected with an error, never silently dropped.
- **Privacy.** The CLI publishes results to Tripo's community by default. The server passes `--visibility private` unless you ask for another value.
- **Credit expiry.** Every `cli` result ends with a line like `balance: 5990 credits; 5910 expire 2026-10-30 (in 27 days); plan professional_6k until 2026-10-30`.

### Choosing what to use

The server also sends these instructions to the assistant, so it can pick without being told.

| You want | Use |
|---|---|
| Best looking, detailed, textured model | `tripo_generate` with the default HD model (`v3.1-20260211`) |
| Low-poly, clean topology with a polycount you set | `tripo_generate` with a Smart Mesh model (P-series, for example `P-v1.0-20250506`), `quad`, and `face_limit` |
| One object split into separate pieces | `generate_parts` (cli) |
| New topology on an existing model | `tripo_remesh` (cli) |
| Retexture, restyle or split an existing model | `tripo_texture`, `tripo_stylize`, `tripo_segment` (cli) |
| Animation | `tripo_rig`, then `tripo_retarget` |
| Another file format | `tripo_convert`, or `out` with the right extension on the cli |
| Reference images to feed into a model | `tripo_image` (cli) |
| Which credits you have | `tripo_balance` |

Studio's Smart Mesh toggle is a choice of model, not a separate flag. Studio shows newer Smart Mesh models than the CLI lists: any model id is passed through, so use the id Studio's network request shows.

### Waiting and downloads

Tools wait for the task and, when given `out`, download the result straight away. The api's model URLs expire after about five minutes, so `out` is the reliable way to keep a result. `out` is a file path, or a directory (end it with `/`) to get a generated file name. `nowait` returns the task id at once instead.

## Tests

```sh
python3 -m unittest -v
```

The tests run against a local fake of the API and a mocked CLI, so they need no network, key, credits or account. CI runs them on Linux, macOS and Windows with Python 3.9 and 3.13.

## Not affiliated

This is an unofficial client. Tripo also publishes an official npm CLI and an MCP server for its Blender add-on.
