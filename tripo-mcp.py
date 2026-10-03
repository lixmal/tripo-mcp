#!/usr/bin/env python3
"""A small MCP server for Tripo: generate a model, rig it, retarget clips, convert, download.

Standard library only, spoken over stdio, talking to Tripo's v3 REST API
(https://developers.tripo3d.ai). Every operation is an asynchronous task: the server
submits it, polls until it finishes and, when given an out path, downloads the result.
Model URLs expire after about five minutes, so the download happens as soon as the task
succeeds. A task that outlives the wait is not lost: tripo_task takes its id and fetches it.

The API key (platform.tripo3d.ai, "tsk_...") comes from TRIPO_API_KEY or the file
tripo/key under $XDG_CONFIG_HOME (default ~/.config, or %APPDATA% on Windows), which
tripo_set_key writes with mode 600. TRIPO_API_BASE overrides the API URL.

On the api backend an input is a task id from an earlier call (task_...), an uploaded
file's token (tripo_upload, file_...) or a direct URL. A local path is uploaded for you.

A second backend drives the Go tripo CLI (github.com/vast-enterprise/tripo-cli), which
signs in as a Tripo Studio account and so spends Studio credits, a separate pool from
API credits. Sign in once with `tripo-cli auth login`. The binary is found on PATH, in
GOBIN or GOPATH/bin, or from TRIPO_CLI. On that backend an input is the project id that
tripo_generate prints, and every result ends with the credit balance and when credits
expire. Pick a backend per call with `backend`, or set TRIPO_BACKEND; unset, the api is
used when a key exists and the cli otherwise.

    claude mcp add tripo -- python3 /path/to/tripo-mcp.py
"""

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date
from pathlib import Path

API = os.environ.get("TRIPO_API_BASE", "https://openapi.tripo3d.ai/v3").rstrip("/")
IS_WINDOWS = os.name == "nt"
POLL_SECONDS = 3
WAIT_SECONDS = 600
CLI_SECONDS = 1800
CLI_FORMATS = ("glb", "fbx", "obj", "usdz", "stl", "3mf")
CREDIT_CODE = 2010
CREDIT_HINT = " (Studio credits are a separate pool: try backend cli)"
MODEL = "v3.1-20260211"
FORMATS = ("GLTF", "FBX", "USDZ", "OBJ", "STL", "3MF")
PRESETS = ("preset:idle", "preset:walk", "preset:run", "preset:dive", "preset:climb",
           "preset:jump", "preset:slash", "preset:shoot", "preset:hurt", "preset:fall",
           "preset:turn", "preset:quadruped:walk", "preset:hexapod:walk",
           "preset:octopod:walk", "preset:serpentine:march", "preset:aquatic:march")


class TripoError(Exception):
    pass


def log(msg):
    print(f"[tripo-mcp] {msg}", file=sys.stderr, flush=True)


# --- Tripo ----------------------------------------------------------------------------

def key_file():
    """XDG_CONFIG_HOME if set, %APPDATA% on Windows, ~/.config everywhere else."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base and IS_WINDOWS:
        base = os.environ.get("APPDATA")
    return (Path(base) if base else Path.home() / ".config") / "tripo" / "key"


def key():
    k = os.environ.get("TRIPO_API_KEY", "").strip()
    path = key_file()
    if not k and path.exists():
        k = path.read_text().strip()
    if not k:
        raise TripoError("no Tripo key: create one at platform.tripo3d.ai and pass it to "
                         "tripo_set_key, or export TRIPO_API_KEY")
    return k


def send(req, what):
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode(errors="replace")
        if e.code == 401:
            raise TripoError("Tripo rejected the key (401): check tripo_set_key") from None
        try:
            hint = CREDIT_HINT if json.loads(detail).get("code") == CREDIT_CODE else ""
        except (ValueError, AttributeError):
            hint = ""
        raise TripoError(f"{what}: HTTP {e.code}: {detail}{hint}") from None
    except urllib.error.URLError as e:
        raise TripoError(f"{what}: {e.reason}") from None
    body = json.loads(raw) if raw else {}
    if body.get("code", 0) != 0:
        hint = CREDIT_HINT if body.get("code") == CREDIT_CODE else ""
        raise TripoError(f"{what}: code {body.get('code')}: {body.get('message') or body}{hint}")
    return body.get("data", body)


def call(method, path, body=None):
    headers = {"Accept": "application/json", "Authorization": f"Bearer {key()}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    return send(urllib.request.Request(API + path, data=data, headers=headers, method=method),
                f"{method} {path}")


def upload(path):
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_file():
        raise TripoError(f"no such file: {p}")
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{p.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
    body = head + p.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(API + "/files", data=body, method="POST", headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {key()}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    return send(req, "POST /files")["file_token"]


def source(value):
    """A task id, file token or URL goes through as it is; anything else is a local file."""
    if value.startswith(("task_", "file_", "http://", "https://")):
        return value
    return upload(value)


def wait(task_id, seconds=None):
    seconds = WAIT_SECONDS if seconds is None else seconds
    deadline = time.monotonic() + seconds
    while True:
        t = call("GET", f"/tasks/{task_id}")
        status = t.get("status")
        if status == "success":
            return t
        if status in ("failed", "cancelled"):
            raise TripoError(f"task {task_id} {status}: {t.get('error_message') or t.get('error_code')}")
        if time.monotonic() > deadline:
            raise TripoError(f"task {task_id} is still {status} ({t.get('progress')}%) after "
                             f"{seconds} s; fetch it later with tripo_task")
        time.sleep(POLL_SECONDS)


def fetch(url, out, stem):
    ext = Path(urllib.parse.urlparse(url).path).suffix or ".bin"
    dest = Path(out).expanduser()
    if not dest.is_absolute():
        dest = Path.cwd() / dest
    if out.endswith(("/", os.sep)) or dest.is_dir():
        dest = dest / (stem + ext)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # The result is a presigned CDN URL: it takes no bearer, and must not be sent one.
    with urllib.request.urlopen(url, timeout=300) as resp, open(dest, "wb") as f:
        while chunk := resp.read(1 << 16):
            f.write(chunk)
    log(f"wrote {dest} ({dest.stat().st_size} bytes)")
    return dest


def finish(task_id, out, nowait):
    """Report a submitted task: wait for it and save the model, or hand back the id."""
    if nowait:
        return f"submitted {task_id}; fetch it with tripo_task"
    t = wait(task_id)
    output = t.get("output") or {}
    lines = [f"{task_id} ({t.get('type', '')}) done, {t.get('credits_consumed', '?')} credits"]
    if out and output.get("model_url"):
        dest = fetch(output["model_url"], out, task_id)
        lines.append(f"saved {dest} ({dest.stat().st_size / 1024:.0f} KiB)")
    else:
        lines.append(f"model_url (expires in minutes): {output.get('model_url')}")
    if output.get("rendered_image_url"):
        lines.append(f"preview: {output['rendered_image_url']}")
    return "\n".join(lines)


def submit(path, body, out, nowait):
    task_id = call("POST", path, {k: v for k, v in body.items() if v is not None})["task_id"]
    log(f"{path}: {task_id}")
    return finish(task_id, out, nowait)


def api_unsupported(a, names):
    bad = sorted(n for n in names if a.get(n) is not None)
    if bad:
        raise TripoError(f"the api backend does not support {', '.join(bad)}; use backend cli")


def api_generate(a):
    if a.get("images"):
        raise TripoError("images (multiview) needs backend cli")
    options = {k: a.get(k) for k in (
        "negative_prompt", "model_seed", "texture_seed", "face_limit", "texture", "pbr",
        "texture_quality", "geometry_quality", "auto_size", "quad", "smart_low_poly",
        "export_orientation", "export_uv", "generate_parts")}
    api_unsupported(a, ("style", "style_image"))
    options["model"] = a.get("model", MODEL)
    if a.get("image"):
        path, options["input"] = "/generation/image-to-model", source(a["image"])
    elif a.get("prompt"):
        path, options["prompt"] = "/generation/text-to-model", a["prompt"]
    else:
        raise TripoError("give a prompt (text to model) or an image (image to model)")
    return submit(path, options, a.get("out"), a.get("nowait"))


def api_format(a):
    fmt = a.get("out_format", "fbx")
    if fmt not in ("glb", "fbx"):
        raise TripoError("the api backend exports glb or fbx here; use backend cli for "
                         + ", ".join(CLI_FORMATS))
    return fmt


def api_rig(a):
    return submit("/animations/rig", {
        "input": source(a["input"]),
        "model": a.get("model"),
        "rig_type": a.get("rig_type"),
        "spec": a.get("spec", "mixamo"),
        "out_format": api_format(a),
    }, a.get("out"), a.get("nowait"))


def api_retarget(a):
    clips = a["animations"]
    body = {
        "input": a["input"],
        "out_format": api_format(a),
        "animate_in_place": a.get("animate_in_place"),
        "export_with_geometry": a.get("export_with_geometry"),
        "bake_animation": a.get("bake_animation"),
    }
    body["animation" if len(clips) == 1 else "animations"] = clips[0] if len(clips) == 1 else clips
    return submit("/animations/retarget", body, a.get("out"), a.get("nowait"))


def api_convert(a):
    fmt = a["format"].upper()
    if fmt not in FORMATS:
        raise TripoError(f"format must be one of {', '.join(FORMATS)}")
    body = {k: a.get(k) for k in (
        "quad", "face_limit", "texture_size", "texture_format", "bake", "pack_uv",
        "scale_factor", "with_animation", "animate_in_place", "export_orientation",
        "fbx_preset", "pivot_to_center_bottom", "export_vertex_colors")}
    api_unsupported(a, ("texture_packaging",))
    body.update(input=source(a["input"]), format=fmt)
    return submit("/models/convert", body, a.get("out"), a.get("nowait"))


def api_task(a):
    if not a.get("task_id") or a.get("action", "get") != "get":
        raise TripoError("the api backend only fetches a task by task_id; use backend cli for "
                         "list, status and wait")
    return finish(a["task_id"], a.get("out"), False)


# --- Tripo CLI ------------------------------------------------------------------------

def cli_binary():
    explicit = os.environ.get("TRIPO_CLI", "").strip()
    if explicit:
        return explicit
    found = shutil.which("tripo-cli")
    if found:
        return found
    gobin = os.environ.get("GOBIN") or str(Path(os.environ.get("GOPATH") or Path.home() / "go") / "bin")
    exe = Path(gobin) / ("tripo-cli.exe" if IS_WINDOWS else "tripo-cli")
    if exe.is_file():
        return str(exe)
    raise TripoError("the tripo CLI is not installed: go install "
                     "github.com/vast-enterprise/tripo-cli@v0.6.0, then tripo-cli auth login "
                     "(set TRIPO_CLI if the binary is not on PATH)")


def run_cli(args, positional=(), timeout=None):
    """Run the CLI and return its output. Positionals follow `--`, so a prompt may start with -."""
    cmd = [cli_binary(), *args] + (["--", *positional] if positional else [])
    timeout = CLI_SECONDS if timeout is None else timeout
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TripoError(f"the tripo CLI did not finish in {timeout} s ({' '.join(args[:2])})") from None
    except OSError as e:
        raise TripoError(f"cannot run the tripo CLI: {e}") from None
    text = p.stdout + p.stderr
    if p.returncode != 0:
        lines = [x.strip() for x in text.splitlines() if x.strip() and not x.startswith("Status:")]
        msg = next((x for x in reversed(lines) if x.startswith("ERROR")),
                   lines[-1] if lines else f"exit code {p.returncode}")
        if any(h in text.lower() for h in ("not logged in", "session expired", "auth login")):
            msg += " (sign in with: tripo-cli auth login)"
        raise TripoError(msg)
    return text


def cli_json(args, positional=()):
    text = run_cli([*args, "--output-format", "json"], positional)
    start = text.find("{")
    if start < 0:
        raise TripoError(f"the tripo CLI printed no JSON: {text.strip()[:200]}")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def credits_text(b, today=None):
    w = b.get("wallet") or {}
    parts = [f"{w.get('total_credit', '?')} credits"]
    exp, when = w.get("expiring_credit"), str(w.get("expiring_date") or "")[:10]
    if exp and when:
        try:
            days = (date.fromisoformat(when) - (today or date.today())).days
            left = f"in {days} days" if days >= 0 else f"{-days} days ago"
        except ValueError:
            left = "date unreadable"
        parts.append(f"{exp} expire {when} ({left})")
    member = b.get("member") or {}
    if member.get("type"):
        parts.append(f"plan {member['type']} until {member.get('valid_until', '?')}")
    return "; ".join(parts)


def cli_footer():
    try:
        return "balance: " + credits_text(cli_json(["account", "balance"]))
    except (TripoError, ValueError):
        return None


def cli_field(text, pattern):
    m = re.search(pattern, text, re.M)
    return m.group(1) if m else None


def cli_result(text, dest):
    lines = []
    if task_id := cli_field(text, r"Task created: (\S+)"):
        lines.append(f"task {task_id}")
    if project := cli_field(text, r"^Project: (\S+)"):
        lines.append(f"project {project} (the input for rig, retarget and convert)")
    if dest and dest.exists():
        lines.append(f"saved {dest} ({dest.stat().st_size / 1024:.0f} KiB)")
    if not lines:
        tail = [x.strip() for x in text.splitlines() if x.strip() and not x.startswith("Status:")]
        lines = tail[-3:] or ["done"]
    if footer := cli_footer():
        lines.append(footer)
    return "\n".join(lines)


def cli_dest(out, stem, ext):
    """The CLI's -o wants a file: a directory (or a trailing slash) gets <stem>.<ext>."""
    dest = Path(out).expanduser()
    if not dest.is_absolute():
        dest = Path.cwd() / dest
    if out.endswith(("/", os.sep)) or dest.is_dir():
        dest = dest / f"{stem}.{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def cli_format(dest, default):
    ext = dest.suffix.lstrip(".").lower()
    return ext if ext in CLI_FORMATS else default


def cli_local(path):
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_file():
        raise TripoError(f"the cli backend takes local image files: {path}")
    return str(p)


def cli_unsupported(a, names):
    bad = sorted(n for n in names if a.get(n) is not None)
    if bad:
        raise TripoError(f"the cli backend does not support {', '.join(bad)}; use backend api")


def cli_generate(a):
    cli_unsupported(a, ("negative_prompt", "model_seed", "texture_seed", "auto_size",
                        "smart_low_poly", "export_orientation", "export_uv"))
    if not a.get("image"):
        cli_unsupported(a, ("style", "style_image"))
    if a.get("images"):
        if len(a["images"]) != 4:
            raise TripoError("images takes four paths: front, back, left, right")
        args, positional = ["generate", "multiview"], [cli_local(p) for p in a["images"]]
    elif a.get("image"):
        args, positional = ["generate", "image"], [cli_local(a["image"])]
        if a.get("prompt"):
            args += ["--prompt", a["prompt"]]
        if a.get("style"):
            args += ["--style", a["style"]]
        if a.get("style_image"):
            args += ["--style-image", cli_local(a["style_image"])]
    elif a.get("prompt"):
        args, positional = ["generate", "text"], [a["prompt"]]
    else:
        raise TripoError("give a prompt, an image, or four images")
    # The CLI publishes to Tripo's community by default; keep results private unless asked.
    args += ["--visibility", a.get("visibility", "private")]
    for flag, name in (("--model-version", "model"), ("--texture-quality", "texture_quality"),
                       ("--geometry-quality", "geometry_quality"), ("--face-limit", "face_limit")):
        if a.get(name) is not None:
            args += [flag, str(a[name])]
    for flag, name in (("--texture", "texture"), ("--pbr", "pbr"), ("--quad", "quad"),
                       ("--generate-parts", "generate_parts")):
        if a.get(name) is not None:
            args.append(f"{flag}={str(a[name]).lower()}")
    dest = None
    if a.get("nowait"):
        pass
    elif a.get("out"):
        dest = cli_dest(a["out"], f"tripo-{int(time.time())}", "glb")
        args += ["--format", cli_format(dest, "glb"), "-o", str(dest)]
    else:
        args.append("--wait")
    return cli_result(run_cli(args, positional), dest)


def cli_process(verb, a, extra):
    """Shared by rig and animate: both take a project id, a format and an output file."""
    fmt = a.get("out_format", "fbx")
    if fmt not in CLI_FORMATS:
        raise TripoError(f"out_format must be one of {', '.join(CLI_FORMATS)}")
    args = ["process", verb, "--format", fmt, *extra]
    dest = None
    if a.get("nowait"):
        args.append("--submit-only")
    elif a.get("out"):
        dest = cli_dest(a["out"], f"{verb}-{int(time.time())}", fmt)
        args += ["-o", str(dest)]
    return cli_result(run_cli(args, [a["input"]]), dest)


def cli_rig(a):
    cli_unsupported(a, ("spec",))
    check = run_cli(["process", "rig", "--check"], [a["input"]])
    if not re.search(r"Riggable:\s*true", check, re.I):
        lines = [x.strip() for x in check.splitlines() if x.strip()]
        raise TripoError("the model cannot be rigged: " + (lines[-1] if lines else "no answer"))
    extra = []
    if a.get("model"):
        extra += ["--model-version", a["model"]]
    if a.get("rig_type"):
        extra += ["--rig-type", a["rig_type"]]
    return cli_process("rig", a, extra)


def cli_retarget(a):
    cli_unsupported(a, ("animate_in_place", "export_with_geometry", "bake_animation"))
    extra = ["--animations", ",".join(a["animations"])]
    if a.get("model"):
        extra += ["--model-version", a["model"]]
    if a.get("rig_type"):
        extra += ["--rig-type", a["rig_type"]]
    return cli_process("animate", a, extra)


def cli_convert(a):
    cli_unsupported(a, ("quad", "face_limit", "texture_format", "bake", "scale_factor",
                        "animate_in_place", "export_orientation", "fbx_preset",
                        "export_vertex_colors", "nowait"))
    fmt = a["format"].lower()
    if fmt not in CLI_FORMATS:
        raise TripoError(f"the cli backend exports {', '.join(CLI_FORMATS)}")
    args = ["export", "--format", fmt]
    if a.get("texture_size"):
        args += ["--texture-size", str(a["texture_size"])]
    if a.get("export_timeout"):
        args += ["--export-timeout", f"{int(a['export_timeout'])}s"]
    if a.get("operator_id"):
        args += ["--operator-id", a["operator_id"]]
    for flag, name in (("--texture-packaging", "texture_packaging"), ("--model-version", "model")):
        if a.get(name):
            args += [flag, a[name]]
    for flag, name in (("--pack-uv", "pack_uv"), ("--pivot-to-center-bottom", "pivot_to_center_bottom"),
                       ("--with-animation", "with_animation")):
        if a.get(name) is not None:
            args.append(f"{flag}={str(a[name]).lower()}")
    dest = None
    if a.get("out"):
        dest = cli_dest(a["out"], f"export-{int(time.time())}", fmt)
        args += ["-o", str(dest)]
    return cli_result(run_cli(args, [a["input"]]), dest)


def cli_flags(a, *pairs):
    """--flag value for each option that is set."""
    out = []
    for flag, name in pairs:
        if a.get(name) is not None:
            out += [flag, str(a[name])]
    return out


def cli_remesh(a):
    extra = cli_flags(a, ("--model-version", "model"), ("--face-limit", "face_limit"))
    extra += [f"--{n}={str(a[n]).lower()}" for n in ("quad", "bake") if a.get(n) is not None]
    return cli_process("remesh", {"out_format": "glb", **a}, extra)


def cli_segment(a):
    extra = cli_flags(a, ("--model-version", "model"), ("--granularity", "granularity"))
    return cli_process("segment", {"out_format": "glb", **a}, extra)


def cli_stylize(a):
    extra = cli_flags(a, ("--model-version", "model"), ("--style", "style"))
    return cli_process("stylize", {"out_format": "glb", **a}, extra)


def cli_texture(a):
    extra = cli_flags(a, ("--model-version", "model"), ("--mode", "mode"),
                      ("--quality", "quality"), ("--alignment", "alignment"))
    return cli_process("texture", {"out_format": "glb", **a}, extra)


def cli_task(a):
    action = a.get("action", "get")
    if action not in ("get", "list", "status", "wait"):
        raise TripoError("action must be get, list, status or wait")
    if action == "list":
        args = cli_flags(a, ("--type", "type"), ("--size", "size"), ("--offset", "offset"))
        return run_cli(["task", "list", *args, "--output-format", "json"]).strip()
    if not a.get("task_id"):
        raise TripoError(f"task_id is required for {action}")
    if action == "get":
        return json.dumps(cli_json(["task", "get"], [a["task_id"]]), indent=2)
    return run_cli(["task", action, "--output-format", "json"], [a["task_id"]]).strip()


def image_list(a):
    args = cli_flags(a, ("--page", "page"), ("--page-size", "page_size"))
    return run_cli(["image", "list", *args, "--output-format", "json"]).strip()


def image(a):
    """Text or image to image, free within Studio's monthly allowance. Only the cli does it."""
    args = []
    for flag, name in (("-m", "model"), ("--scale", "scale")):
        if a.get(name):
            args += [flag, a[name]]
    if a.get("input"):
        args += ["-i", cli_local(a["input"])]
    if a.get("sketch"):
        args.append("--sketch")
    if a.get("amount") is not None:
        if not 1 <= a["amount"] <= 4:
            raise TripoError("amount must be 1 to 4")
        args += ["--amount", str(a["amount"])]
    text = run_cli(["image", *args], [a["prompt"]])
    assets = re.findall(r"Task created: (\S+)", text)
    if not assets:
        raise TripoError(f"the tripo CLI did not report a task: {text.strip()[-200:]}")
    lines = []
    for asset in assets:
        # The CLI's own -o writes a single image to a file path only, so the download is done here.
        url = cli_json(["image", "get"], [asset]).get("url")
        lines.append(f"image {asset}")
        if url and a.get("out"):
            dest = fetch(url, a["out"], asset)
            lines.append(f"saved {dest} ({dest.stat().st_size / 1024:.0f} KiB)")
        elif url:
            lines.append(f"url (expires in minutes): {url}")
    if footer := cli_footer():
        lines.append(footer)
    return "\n".join(lines)


# --- Backend choice -------------------------------------------------------------------

def pick_backend(a):
    b = (a.get("backend") or os.environ.get("TRIPO_BACKEND") or "").strip().lower()
    if b in ("api", "cli"):
        return b
    if b:
        raise TripoError("backend must be api or cli")
    try:
        key()
        return "api"
    except TripoError:
        return "cli"


def either(api, cli):
    return lambda a: (cli if pick_backend(a) == "cli" else api)(a)


generate = either(api_generate, cli_generate)
rig = either(api_rig, cli_rig)
retarget = either(api_retarget, cli_retarget)
convert = either(api_convert, cli_convert)
task = either(api_task, cli_task)


def balance(a):
    if pick_backend(a) == "cli":
        return credits_text(cli_json(["account", "balance"]))
    return json.dumps(call("GET", "/account/balance"), indent=2)


def set_key(value):
    value = value.strip()
    if not value:
        raise TripoError("empty key")
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # mode is ignored on Windows
    with os.fdopen(fd, "w") as f:
        f.write(value)
    return f"key saved to {path}"


# --- MCP ------------------------------------------------------------------------------

OUT = {"type": "string", "description": "file path, or a directory (ending in /) to save as "
                                         "<task id>.<ext>; relative paths are from the server's "
                                         "working directory. Omit to get the short-lived URL."}
NOWAIT = {"type": "boolean", "default": False,
          "description": "return the task id at once instead of waiting; fetch with tripo_task"}
INPUT = {"type": "string", "description": "api backend: task id (task_...), file token (file_...), "
                                           "URL, or a local file path, which is uploaded. cli "
                                           "backend: the project id from tripo_generate"}
BACKEND = {"type": "string", "enum": ["api", "cli"],
           "description": "api spends API credits; cli drives the tripo CLI and spends Studio "
                          "credits. Default: TRIPO_BACKEND, else api when a key exists, else cli"}

CLI_INPUT = {"type": "string", "description": "the project id from tripo_generate"}
CLI_MODEL = {"type": "string", "description": "model version"}
CLI_FORMAT = {"type": "string", "enum": list(CLI_FORMATS), "default": "glb"}

TOOLS = [
    {
        "name": "tripo_set_key",
        "description": "Store the Tripo API key (tsk_..., from platform.tripo3d.ai).",
        "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}},
                        "required": ["key"]},
        "run": lambda a: set_key(a["key"]),
    },
    {
        "name": "tripo_upload",
        "description": "Upload a local image (20 MB) or model (150 MB) and return its file token.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"]},
        "run": lambda a: upload(a["path"]),
    },
    {
        "name": "tripo_generate",
        "description": "Make a 3D model from a text prompt, an image, or (cli only) four view "
                       "images. Costs credits (about 25 each on the cli). Saves a textured GLB "
                       "when out is given.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "backend": BACKEND,
                "prompt": {"type": "string", "description": "up to 1024 characters; with an image on "
                                                            "the cli it is an optional guiding prompt"},
                "image": {**INPUT, "description": "image source for image to model "
                                                  "(PNG, JPEG or WebP, at least 256 px); the "
                                                  "cli backend takes local files only"},
                "images": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "string"},
                           "description": "cli only: front, back, left and right view files"},
                "visibility": {"type": "string", "enum": ["public", "private", "shareable"],
                               "default": "private",
                               "description": "cli only; the CLI itself defaults to public"},
                "style": {"type": "string", "description": "cli only, image input only: style preset"},
                "style_image": {"type": "string", "description": "cli only, image input only: "
                                                                 "local style reference image"},
                "negative_prompt": {"type": "string"},
                "model": {"type": "string", "default": MODEL,
                          "description": "v3.1-20260211 (HD), v3.0-20250812, v2.5-20250123, "
                                         "Nexus-v1.0-20260214, or a Smart Mesh P-series id such "
                                         "as P-v1.0-20250506; any id the backend accepts passes "
                                         "through"},
                "generate_parts": {"type": "boolean", "description": "generate as multiple parts"},
                "texture": {"type": "boolean", "default": True},
                "pbr": {"type": "boolean", "default": True},
                "texture_quality": {"type": "string", "enum": ["fast", "standard", "detailed", "extreme"]},
                "geometry_quality": {"type": "string", "enum": ["standard", "detailed", "original"]},
                "face_limit": {"type": "integer"},
                "auto_size": {"type": "boolean", "description": "scale to real-world meters"},
                "quad": {"type": "boolean", "description": "quad mesh; forces FBX"},
                "smart_low_poly": {"type": "boolean"},
                "export_orientation": {"type": "string", "enum": ["+x", "-x", "+y", "-y"]},
                "export_uv": {"type": "boolean"},
                "model_seed": {"type": "integer"},
                "texture_seed": {"type": "integer"},
                "out": OUT,
                "nowait": NOWAIT,
            },
        },
        "run": generate,
    },
    {
        "name": "tripo_rig",
        "description": "Add a skeleton to a model. spec mixamo gives Mixamo bone names, which "
                       "Unity maps to Humanoid. Defaults to FBX.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "backend": BACKEND,
                "input": INPUT,
                "model": {"type": "string", "enum": ["v1.0-20240301", "v2.5-20260210"],
                          "description": "v1.0 is biped only, v2.5 takes other creatures"},
                "rig_type": {"type": "string", "enum": [
                    "biped", "quadruped", "hexapod", "octopod", "avian", "serpentine", "aquatic"]},
                "spec": {"type": "string", "enum": ["tripo", "mixamo"], "default": "mixamo"},
                "out_format": {"type": "string", "enum": list(CLI_FORMATS), "default": "fbx",
                               "description": "the api backend exports glb or fbx only"},
                "out": OUT,
                "nowait": NOWAIT,
            },
            "required": ["input"],
        },
        "run": rig,
    },
    {
        "name": "tripo_retarget",
        "description": "Apply preset animations to a rigged model. Defaults to FBX. api: input is "
                       "the task id of the rig task and clips are named " + ", ".join(PRESETS) +
                       ". cli: input is the project id and clips are named like preset:biped:run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "backend": BACKEND,
                "input": {"type": "string", "description": "rig task id (api) or project id (cli)"},
                "animations": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                "rig_type": {"type": "string", "description": "cli only, default biped"},
                "model": {"type": "string", "description": "cli only: animation model version"},
                "out_format": {"type": "string", "enum": list(CLI_FORMATS), "default": "fbx",
                               "description": "the api backend exports glb or fbx only"},
                "animate_in_place": {"type": "boolean"},
                "export_with_geometry": {"type": "boolean"},
                "bake_animation": {"type": "boolean", "description": "GLB only"},
                "out": OUT,
                "nowait": NOWAIT,
            },
            "required": ["input", "animations"],
        },
        "run": retarget,
    },
    {
        "name": "tripo_convert",
        "description": "Convert a model to another format with optional mesh and texture "
                       "processing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "backend": BACKEND,
                "input": INPUT,
                "format": {"type": "string", "enum": list(FORMATS),
                           "description": "the cli backend exports glb, fbx, obj, usdz, stl, 3mf"},
                "quad": {"type": "boolean"},
                "face_limit": {"type": "integer"},
                "texture_size": {"type": "integer", "default": 4096},
                "texture_format": {"type": "string", "default": "JPEG"},
                "operator_id": {"type": "string", "description": "cli only: download this "
                                                                "operator's model, skipping export"},
                "export_timeout": {"type": "integer", "description": "cli only: seconds"},
                "texture_packaging": {"type": "string", "enum": ["embedded", "zip"],
                                      "description": "cli only"},
                "model": {"type": "string", "description": "cli only: export model version"},
                "bake": {"type": "boolean", "default": True},
                "pack_uv": {"type": "boolean"},
                "scale_factor": {"type": "number", "default": 1},
                "with_animation": {"type": "boolean", "default": True},
                "animate_in_place": {"type": "boolean"},
                "export_orientation": {"type": "string", "enum": ["+x", "-x", "+y", "-y"]},
                "fbx_preset": {"type": "string", "enum": ["blender", "3dsmax", "mixamo", "bake_scale"]},
                "pivot_to_center_bottom": {"type": "boolean"},
                "export_vertex_colors": {"type": "boolean"},
                "out": OUT,
                "nowait": NOWAIT,
            },
            "required": ["input", "format"],
        },
        "run": convert,
    },
    {
        "name": "tripo_task",
        "description": "api: wait for a task submitted earlier and save its model. cli: show the "
                       "task's status and details.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "backend": BACKEND,
                "task_id": {"type": "string"},
                "action": {"type": "string", "enum": ["get", "list", "status", "wait"],
                           "default": "get", "description": "cli only: list needs no task_id; "
                                                            "status is one progress check; "
                                                            "wait polls until the task ends"},
                "type": {"type": "string", "enum": ["all", "textured", "untextured", "rigged"],
                         "description": "cli list filter"},
                "size": {"type": "integer", "description": "cli list page size"},
                "offset": {"type": "integer", "description": "cli list offset"},
                "out": OUT,
            },
        },
        "run": task,
    },
    {
        "name": "tripo_remesh",
        "description": "Retopologize a model. cli backend only. Input is the project id.",
        "inputSchema": {"type": "object", "properties": {
            "input": CLI_INPUT, "quad": {"type": "boolean"}, "face_limit": {"type": "integer"},
            "bake": {"type": "boolean", "description": "bake textures after remesh"},
            "model": CLI_MODEL, "out_format": CLI_FORMAT, "out": OUT, "nowait": NOWAIT},
            "required": ["input"]},
        "run": cli_remesh,
    },
    {
        "name": "tripo_segment",
        "description": "Split a model into semantic parts. cli backend only.",
        "inputSchema": {"type": "object", "properties": {
            "input": CLI_INPUT,
            "granularity": {"type": "string", "enum": ["simple", "balanced", "detailed"]},
            "model": CLI_MODEL, "out_format": CLI_FORMAT, "out": OUT, "nowait": NOWAIT},
            "required": ["input"]},
        "run": cli_segment,
    },
    {
        "name": "tripo_stylize",
        "description": "Apply a style preset to a model. cli backend only.",
        "inputSchema": {"type": "object", "properties": {
            "input": CLI_INPUT, "style": {"type": "string", "description": "style name"},
            "model": CLI_MODEL, "out_format": CLI_FORMAT, "out": OUT, "nowait": NOWAIT},
            "required": ["input", "style"]},
        "run": cli_stylize,
    },
    {
        "name": "tripo_texture",
        "description": "Generate, redo or add PBR to the textures of a model. cli backend only.",
        "inputSchema": {"type": "object", "properties": {
            "input": CLI_INPUT,
            "mode": {"type": "string", "enum": ["generate", "retexture", "pbr"]},
            "quality": {"type": "string", "enum": ["standard", "detailed", "extreme"]},
            "alignment": {"type": "string", "enum": ["standard", "detailed"],
                          "description": "generate mode only"},
            "model": CLI_MODEL, "out_format": CLI_FORMAT, "out": OUT, "nowait": NOWAIT},
            "required": ["input"]},
        "run": cli_texture,
    },
    {
        "name": "tripo_image_list",
        "description": "List generated images. cli backend only.",
        "inputSchema": {"type": "object", "properties": {
            "page": {"type": "integer"}, "page_size": {"type": "integer", "maximum": 100}}},
        "run": image_list,
    },
    {
        "name": "tripo_image",
        "description": "Generate an image from a prompt, optionally from an input image or a "
                       "sketch. cli backend only; free within Studio's monthly image allowance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "input": {"type": "string", "description": "local image to transform"},
                "model": {"type": "string", "enum": [
                    "gemini_2.5_flash_image_preview", "gemini_3_pro_image_preview",
                    "gemini_3.1_flash_image_preview", "midjourney", "gpt_image_1.5", "gpt_image_2"]},
                "scale": {"type": "string", "enum": ["1:1", "3:4", "4:3", "16:9", "9:16"]},
                "sketch": {"type": "boolean", "description": "treat input as a sketch to render"},
                "amount": {"type": "integer", "minimum": 1, "maximum": 4,
                           "description": "number of images to generate (default 1)"},
                "out": {**OUT, "description": "file path, or a directory (ending in /) to save "
                                              "as <asset id>.png; use a directory when amount > 1. Omit to get the short-lived URL."},
            },
            "required": ["prompt"],
        },
        "run": image,
    },
    {
        "name": "tripo_balance",
        "description": "Show the credit balance. cli: includes how many credits expire and "
                       "when, and the plan. api: the raw account balance.",
        "inputSchema": {"type": "object", "properties": {"backend": BACKEND}},
        "run": balance,
    },
]
BY_NAME = {t["name"]: t for t in TOOLS}


def handle(msg):
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tripo", "version": "1.1.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{k: v for k, v in t.items() if k != "run"} for t in TOOLS]}
    if method == "tools/call":
        tool = BY_NAME.get(params.get("name"))
        if tool is None:
            raise LookupError(f"unknown tool {params.get('name')}")
        try:
            text, is_error = tool["run"](params.get("arguments") or {}), False
        except (TripoError, OSError, KeyError, ValueError) as e:
            text, is_error = str(e), True
        return {"content": [{"type": "text", "text": text}], "isError": is_error}
    raise LookupError(f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            print(json.dumps(reply), flush=True)
            continue
        if "id" not in msg:
            continue  # a notification: nothing is answered
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
        except LookupError as e:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": str(e)}}
        except Exception as e:  # never let one request take the server down
            log(f"{msg.get('method')}: {e!r}")
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
