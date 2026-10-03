"""Tests for tripo-mcp.py against a local fake of Tripo's v3 API: no network, no credits.

    python3 -m unittest -v
"""

import importlib.util
import io
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location("tripo_mcp", HERE / "tripo-mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tripo = load()
MODEL_BYTES = b"glTF-fake-model"


class FakeTripo(BaseHTTPRequestHandler):
    """Records every request and answers like the v3 API; behavior is set per test."""

    requests = []
    statuses = []  # successive task statuses; the last one repeats
    credits = True

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def record(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        FakeTripo.requests.append({
            "method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
            "ctype": self.headers.get("Content-Type", ""), "body": body})
        return body

    def do_POST(self):
        body = self.record()
        if self.headers.get("Authorization") != "Bearer tsk_test":
            return self.reply(401, {"code": 1001, "message": "bad key"})
        if self.path == "/files":
            return self.reply(200, {"code": 0, "data": {"file_token": "file_abc"}})
        if not FakeTripo.credits:
            return self.reply(403, {"code": 2010, "message": "You don't have enough credit",
                                    "suggestion": "Please purchase more credit"})
        json.loads(body)
        self.reply(200, {"code": 0, "data": {"task_id": "task_1"}})

    def do_GET(self):
        self.record()
        if self.path == "/cdn/model.glb":
            self.send_response(200)
            self.send_header("Content-Length", str(len(MODEL_BYTES)))
            self.end_headers()
            self.wfile.write(MODEL_BYTES)
            return
        status = FakeTripo.statuses.pop(0) if len(FakeTripo.statuses) > 1 else FakeTripo.statuses[0]
        data = {"task_id": "task_1", "type": "image_to_model", "status": status, "progress": 50,
                "credits_consumed": 30}
        if status == "success":
            data["output"] = {"model_url": f"http://127.0.0.1:{self.server.server_port}/cdn/model.glb",
                              "rendered_image_url": "http://example.invalid/p.png"}
        if status == "failed":
            data["error_message"] = "bad input"
        self.reply(200, {"code": 0, "data": data})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTripo)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        FakeTripo.requests = []
        FakeTripo.statuses = ["success"]
        FakeTripo.credits = True
        env = mock.patch.dict(os.environ, {"TRIPO_API_KEY": "tsk_test", "XDG_CONFIG_HOME": self.tmp.name,
                                           "TRIPO_BACKEND": "", "TRIPO_CLI": "fake-tripo"})
        env.start()
        self.addCleanup(env.stop)
        for patch in (mock.patch.object(tripo, "API", f"http://127.0.0.1:{self.server.server_port}"),
                      mock.patch.object(tripo, "POLL_SECONDS", 0),
                      mock.patch.object(tripo, "log", lambda msg: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def tool(self, name, **args):
        return tripo.BY_NAME[name]["run"](args)

    def posts(self):
        return [r for r in FakeTripo.requests if r["method"] == "POST"]

    def last_body(self):
        return json.loads(self.posts()[-1]["body"])


class TestKey(Base):
    def test_env_wins_over_file(self):
        path = tripo.key_file()
        path.parent.mkdir(parents=True)
        path.write_text("tsk_file\n")
        self.assertEqual(tripo.key(), "tsk_test")

    def test_file_used_without_env(self):
        with mock.patch.dict(os.environ):
            del os.environ["TRIPO_API_KEY"]
            self.tool("tripo_set_key", key=" tsk_saved ")
            self.assertEqual(tripo.key(), "tsk_saved")

    def test_missing_key_names_the_fix(self):
        with mock.patch.dict(os.environ):
            del os.environ["TRIPO_API_KEY"]
            with self.assertRaisesRegex(tripo.TripoError, "tripo_set_key"):
                tripo.key()

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_saved_key_is_private(self):
        self.tool("tripo_set_key", key="tsk_x")
        self.assertEqual(tripo.key_file().stat().st_mode & 0o777, 0o600)

    def test_key_file_location_per_platform(self):
        home = Path(self.tmp.name) / "home"
        with mock.patch.dict(os.environ, clear=True), mock.patch.object(Path, "home", return_value=home):
            self.assertEqual(tripo.key_file(), home / ".config" / "tripo" / "key")
            os.environ["XDG_CONFIG_HOME"] = str(home / "xdg")
            self.assertEqual(tripo.key_file(), home / "xdg" / "tripo" / "key")
        appdata = Path(self.tmp.name) / "AppData"
        with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=True), \
                mock.patch.object(tripo, "IS_WINDOWS", True):
            self.assertEqual(tripo.key_file(), appdata / "tripo" / "key")

    def test_empty_key_rejected(self):
        with self.assertRaises(tripo.TripoError):
            self.tool("tripo_set_key", key="  ")


class TestSource(Base):
    def test_passthrough_forms(self):
        for v in ("task_1", "file_9", "http://x/y.png", "https://x/y.png"):
            self.assertEqual(tripo.source(v), v)
        self.assertEqual(FakeTripo.requests, [])

    def test_local_file_is_uploaded_as_multipart(self):
        img = Path(self.tmp.name) / "a.png"
        img.write_bytes(b"\x89PNG-bytes")
        self.assertEqual(tripo.source(str(img)), "file_abc")
        req = self.posts()[0]
        self.assertEqual(req["path"], "/files")
        self.assertTrue(req["ctype"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'filename="a.png"', req["body"])
        self.assertIn(b"\x89PNG-bytes", req["body"])
        self.assertIn(b"Content-Type: image/png", req["body"])

    def test_missing_file(self):
        with self.assertRaisesRegex(tripo.TripoError, "no such file"):
            tripo.source(str(Path(self.tmp.name) / "nope.png"))


class TestGenerate(Base):
    def test_text_to_model_downloads_into_directory(self):
        out = self.tool("tripo_generate", prompt="a cat", out=self.tmp.name + os.sep)
        self.assertEqual(self.posts()[0]["path"], "/generation/text-to-model")
        self.assertEqual(self.last_body(), {"prompt": "a cat", "model": tripo.MODEL})
        saved = Path(self.tmp.name) / "task_1.glb"
        self.assertEqual(saved.read_bytes(), MODEL_BYTES)
        self.assertIn(str(saved), out)
        self.assertIn("30 credits", out)

    def test_the_cdn_download_is_sent_no_bearer(self):
        self.tool("tripo_generate", prompt="a cat", out=self.tmp.name + os.sep)
        cdn = [r for r in FakeTripo.requests if r["path"] == "/cdn/model.glb"]
        self.assertEqual(len(cdn), 1)
        self.assertIsNone(cdn[0]["auth"])

    def test_image_to_model_uploads_then_submits_the_token(self):
        img = Path(self.tmp.name) / "front.jpg"
        img.write_bytes(b"jpg")
        self.tool("tripo_generate", image=str(img), texture_quality="detailed", nowait=True)
        self.assertEqual([p["path"] for p in self.posts()], ["/files", "/generation/image-to-model"])
        self.assertEqual(self.last_body(),
                         {"input": "file_abc", "model": tripo.MODEL, "texture_quality": "detailed"})

    def test_needs_prompt_or_image(self):
        with self.assertRaisesRegex(tripo.TripoError, "prompt"):
            self.tool("tripo_generate")

    def test_explicit_file_path_for_out(self):
        dest = Path(self.tmp.name) / "sub" / "rill.glb"
        self.tool("tripo_generate", prompt="x", out=str(dest))
        self.assertEqual(dest.read_bytes(), MODEL_BYTES)

    def test_no_out_returns_the_url(self):
        out = self.tool("tripo_generate", prompt="x")
        self.assertIn("/cdn/model.glb", out)

    def test_nowait_returns_the_task_id_without_polling(self):
        out = self.tool("tripo_generate", prompt="x", nowait=True)
        self.assertIn("task_1", out)
        self.assertFalse([r for r in FakeTripo.requests if r["method"] == "GET"])

    def test_polls_until_success(self):
        FakeTripo.statuses = ["queued", "running", "running", "success"]
        self.tool("tripo_generate", prompt="x", out=self.tmp.name + os.sep)
        polls = [r for r in FakeTripo.requests if r["path"] == "/tasks/task_1"]
        self.assertEqual(len(polls), 4)

    def test_failed_task_reports_the_message(self):
        FakeTripo.statuses = ["failed"]
        with self.assertRaisesRegex(tripo.TripoError, "failed: bad input"):
            self.tool("tripo_generate", prompt="x")

    def test_timeout_points_at_tripo_task(self):
        FakeTripo.statuses = ["running"]
        with mock.patch.object(tripo, "WAIT_SECONDS", -1):
            with self.assertRaisesRegex(tripo.TripoError, "tripo_task"):
                self.tool("tripo_generate", prompt="x")

    def test_out_of_credits_is_surfaced(self):
        FakeTripo.credits = False
        with self.assertRaisesRegex(tripo.TripoError, "enough credit"):
            self.tool("tripo_generate", prompt="x")

    def test_bad_key_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {"TRIPO_API_KEY": "tsk_wrong"}):
            with self.assertRaisesRegex(tripo.TripoError, "rejected the key"):
                self.tool("tripo_generate", prompt="x")

    def test_tripo_task_fetches_an_earlier_task(self):
        out = self.tool("tripo_task", task_id="task_1", out=self.tmp.name + os.sep)
        self.assertEqual((Path(self.tmp.name) / "task_1.glb").read_bytes(), MODEL_BYTES)
        self.assertIn("done", out)


class TestProcessing(Base):
    def test_rig_defaults_to_mixamo_fbx(self):
        self.tool("tripo_rig", input="task_7", nowait=True)
        self.assertEqual(self.posts()[-1]["path"], "/animations/rig")
        self.assertEqual(self.last_body(), {"input": "task_7", "spec": "mixamo", "out_format": "fbx"})

    def test_retarget_one_clip_uses_animation(self):
        self.tool("tripo_retarget", input="task_7", animations=["preset:walk"], nowait=True)
        self.assertEqual(self.last_body(),
                         {"input": "task_7", "out_format": "fbx", "animation": "preset:walk"})

    def test_retarget_many_clips_uses_animations(self):
        clips = ["preset:walk", "preset:run"]
        self.tool("tripo_retarget", input="task_7", animations=clips, animate_in_place=True, nowait=True)
        body = self.last_body()
        self.assertEqual(body["animations"], clips)
        self.assertNotIn("animation", body)
        self.assertTrue(body["animate_in_place"])

    def test_convert_uppercases_format_and_drops_unset_options(self):
        self.tool("tripo_convert", input="task_7", format="fbx", fbx_preset="mixamo", nowait=True)
        self.assertEqual(self.posts()[-1]["path"], "/models/convert")
        self.assertEqual(self.last_body(), {"input": "task_7", "format": "FBX", "fbx_preset": "mixamo"})

    def test_convert_rejects_unknown_format(self):
        with self.assertRaisesRegex(tripo.TripoError, "format must be"):
            self.tool("tripo_convert", input="task_7", format="dwg")


BALANCE = json.dumps({
    "member": {"type": "professional_6k", "valid_until": "2026-10-30"},
    "wallet": {"total_credit": 5990, "expiring_credit": 5910, "expiring_date": "2026-10-30"}})


class CliBase(Base):
    """The CLI backend with subprocess.run replaced: the real binary and account are never touched."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.responses = {("account", "balance"): (0, BALANCE)}
        run = mock.patch.object(tripo.subprocess, "run", side_effect=self.fake_run)
        run.start()
        self.addCleanup(run.stop)

    def fake_run(self, cmd, **kwargs):
        self.assertEqual(cmd[0], "fake-tripo")
        args = cmd[1:]
        self.calls.append(args)
        rc, out = self.responses.get(tuple(args[:2])) or self.responses.get(tuple(args[:1])) or (0, "")
        return tripo.subprocess.CompletedProcess(cmd, rc, out, "")

    def call_for(self, *prefix):
        return next(c for c in self.calls if tuple(c[:len(prefix)]) == prefix)

    def cli(self, name, **args):
        return self.tool(name, backend="cli", **args)


class TestBackendChoice(Base):
    def test_explicit_argument_wins(self):
        self.assertEqual(tripo.pick_backend({"backend": "cli"}), "cli")
        self.assertEqual(tripo.pick_backend({"backend": "api"}), "api")

    def test_env_default(self):
        with mock.patch.dict(os.environ, {"TRIPO_BACKEND": "CLI"}):
            self.assertEqual(tripo.pick_backend({}), "cli")

    def test_api_when_a_key_exists_and_cli_without(self):
        self.assertEqual(tripo.pick_backend({}), "api")
        with mock.patch.dict(os.environ):
            del os.environ["TRIPO_API_KEY"]
            self.assertEqual(tripo.pick_backend({}), "cli")

    def test_unknown_backend(self):
        with self.assertRaisesRegex(tripo.TripoError, "api or cli"):
            tripo.pick_backend({"backend": "web"})

    def test_out_of_api_credits_points_at_the_cli(self):
        FakeTripo.credits = False
        with self.assertRaisesRegex(tripo.TripoError, "backend cli"):
            self.tool("tripo_generate", prompt="x")

    def test_credit_error_hint_matches_the_real_compact_body(self):
        """Tripo's body has no space after the colon; the hint must not depend on the formatting."""
        raw = b'{"code":2010,"status":"error","message":"You don\'t have enough credit"}'
        err = tripo.urllib.error.HTTPError("http://x", 403, "Forbidden", {}, io.BytesIO(raw))
        with mock.patch.object(tripo.urllib.request, "urlopen", side_effect=err):
            with self.assertRaisesRegex(tripo.TripoError, "backend cli"):
                tripo.send(tripo.urllib.request.Request("http://x"), "POST /x")

    def test_other_http_errors_get_no_credit_hint(self):
        err = tripo.urllib.error.HTTPError("http://x", 500, "Err", {}, io.BytesIO(b"not json"))
        with mock.patch.object(tripo.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(tripo.TripoError) as ctx:
                tripo.send(tripo.urllib.request.Request("http://x"), "POST /x")
        self.assertNotIn("backend cli", str(ctx.exception))

    def test_missing_binary_says_how_to_install(self):
        with mock.patch.dict(os.environ, {"TRIPO_CLI": "", "GOBIN": self.tmp.name}), \
                mock.patch.object(tripo.shutil, "which", return_value=None):
            with self.assertRaisesRegex(tripo.TripoError, "go install"):
                tripo.cli_binary()


class TestCredits(unittest.TestCase):
    def test_expiry_is_shown_with_days_left(self):
        text = tripo.credits_text(json.loads(BALANCE), today=tripo.date(2026, 10, 3))
        self.assertIn("5990 credits", text)
        self.assertIn("5910 expire 2026-10-30 (in 27 days)", text)
        self.assertIn("professional_6k", text)

    def test_already_expired(self):
        text = tripo.credits_text(json.loads(BALANCE), today=tripo.date(2026, 11, 2))
        self.assertIn("(3 days ago)", text)

    def test_no_expiring_credits(self):
        text = tripo.credits_text({"wallet": {"total_credit": 10, "expiring_credit": 0}})
        self.assertEqual(text, "10 credits")

    def test_timestamp_style_date_is_read(self):
        b = {"wallet": {"total_credit": 1, "expiring_credit": 1, "expiring_date": "2026-10-30T00:00:00Z"}}
        self.assertIn("(in 27 days)", tripo.credits_text(b, today=tripo.date(2026, 10, 3)))


class TestCliGenerate(CliBase):
    def test_text_is_private_by_default_and_after_the_separator(self):
        self.responses[("generate", "text")] = (0, "OK: Task created: t-1\nProject: p-1\n")
        out = self.cli("tripo_generate", prompt="-a cat", nowait=True)
        call = self.call_for("generate", "text")
        self.assertEqual(call[call.index("--visibility") + 1], "private")
        self.assertEqual(call[-2:], ["--", "-a cat"])
        self.assertNotIn("--wait", call)
        self.assertNotIn("-o", call)
        self.assertIn("task t-1", out)
        self.assertIn("project p-1", out)

    def test_every_result_ends_with_the_balance_and_expiry(self):
        self.responses[("generate", "text")] = (0, "OK: Task created: t-1\n")
        out = self.cli("tripo_generate", prompt="x", nowait=True)
        self.assertIn("balance: 5990 credits; 5910 expire 2026-10-30", out)

    def test_balance_failure_does_not_fail_the_task(self):
        self.responses[("generate", "text")] = (0, "OK: Task created: t-1\n")
        self.responses[("account", "balance")] = (1, "ERROR: boom")
        out = self.cli("tripo_generate", prompt="x", nowait=True)
        self.assertIn("task t-1", out)
        self.assertNotIn("balance", out)

    def test_out_file_sets_format_and_download_path(self):
        dest = Path(self.tmp.name) / "rill.fbx"
        dest.write_bytes(b"fbx")  # the real CLI writes it; the fake does not
        out = self.cli("tripo_generate", prompt="x", out=str(dest))
        call = self.call_for("generate", "text")
        self.assertEqual(call[call.index("--format") + 1], "fbx")
        self.assertEqual(call[call.index("-o") + 1], str(dest))
        self.assertIn(f"saved {dest}", out)

    def test_directory_out_gets_a_generated_name(self):
        self.cli("tripo_generate", prompt="x", out=self.tmp.name + os.sep)
        path = Path(self.call_for("generate", "text")[self.call_for("generate", "text").index("-o") + 1])
        self.assertEqual(path.parent, Path(self.tmp.name))
        self.assertRegex(path.name, r"^tripo-\d+\.glb$")

    def test_no_out_still_waits(self):
        self.cli("tripo_generate", prompt="x")
        self.assertIn("--wait", self.call_for("generate", "text"))

    def test_booleans_and_options_are_passed(self):
        self.cli("tripo_generate", prompt="x", nowait=True, texture=False, pbr=True, quad=True,
                 face_limit=5000, texture_quality="detailed", model="v3.0-20250812", visibility="shareable")
        call = self.call_for("generate", "text")
        for expected in ("--texture=false", "--pbr=true", "--quad=true"):
            self.assertIn(expected, call)
        self.assertEqual(call[call.index("--face-limit") + 1], "5000")
        self.assertEqual(call[call.index("--texture-quality") + 1], "detailed")
        self.assertEqual(call[call.index("--model-version") + 1], "v3.0-20250812")
        self.assertEqual(call[call.index("--visibility") + 1], "shareable")

    def test_four_views_become_multiview(self):
        views = []
        for name in ("front", "back", "left", "right"):
            p = Path(self.tmp.name) / f"{name}.png"
            p.write_bytes(b"x")
            views.append(str(p))
        self.cli("tripo_generate", images=views, nowait=True)
        self.assertEqual(self.call_for("generate", "multiview")[-5:], ["--", *views])

    def test_wrong_number_of_views(self):
        with self.assertRaisesRegex(tripo.TripoError, "four paths"):
            self.cli("tripo_generate", images=["a.png"])

    def test_image_must_be_a_local_file(self):
        with self.assertRaisesRegex(tripo.TripoError, "local image files"):
            self.cli("tripo_generate", image="https://example.com/a.png")

    def test_options_the_cli_lacks_are_refused_not_dropped(self):
        with self.assertRaisesRegex(tripo.TripoError, "model_seed"):
            self.cli("tripo_generate", prompt="x", model_seed=3)
        self.assertEqual(self.calls, [])

    def test_api_backend_refuses_views(self):
        with self.assertRaisesRegex(tripo.TripoError, "backend cli"):
            self.tool("tripo_generate", backend="api", images=["a", "b", "c", "d"])

    def test_nothing_to_generate(self):
        with self.assertRaisesRegex(tripo.TripoError, "prompt"):
            self.cli("tripo_generate")


class TestCliProcessing(CliBase):
    def test_rig_checks_first_and_refuses_when_not_riggable(self):
        self.responses[("process", "rig")] = (0, "Riggable: false\n")
        with self.assertRaisesRegex(tripo.TripoError, "cannot be rigged"):
            self.cli("tripo_rig", input="p-1")
        self.assertEqual([c for c in self.calls if "--check" not in c and c[0] == "process"], [])

    def test_rig_runs_after_a_good_check(self):
        self.responses[("process", "rig")] = (0, "Riggable: true\n")
        self.cli("tripo_rig", input="p-1", rig_type="quadruped", out=self.tmp.name + os.sep)
        rigs = [c for c in self.calls if c[:2] == ["process", "rig"]]
        self.assertIn("--check", rigs[0])
        self.assertNotIn("--check", rigs[1])
        self.assertEqual(rigs[1][rigs[1].index("--rig-type") + 1], "quadruped")
        self.assertEqual(rigs[1][rigs[1].index("--format") + 1], "fbx")
        self.assertEqual(rigs[1][-2:], ["--", "p-1"])

    def test_rig_nowait_submits_only(self):
        self.responses[("process", "rig")] = (0, "Riggable: true\n")
        self.cli("tripo_rig", input="p-1", nowait=True)
        rigs = [c for c in self.calls if c[:2] == ["process", "rig"]]
        self.assertIn("--submit-only", rigs[1])

    def test_rig_spec_is_refused(self):
        with self.assertRaisesRegex(tripo.TripoError, "spec"):
            self.cli("tripo_rig", input="p-1", spec="mixamo")

    def test_retarget_joins_clips(self):
        self.cli("tripo_retarget", input="p-1", animations=["preset:biped:walk", "preset:biped:run"],
                 nowait=True)
        call = self.call_for("process", "animate")
        self.assertEqual(call[call.index("--animations") + 1], "preset:biped:walk,preset:biped:run")
        self.assertIn("--submit-only", call)

    def test_retarget_refuses_api_only_options(self):
        with self.assertRaisesRegex(tripo.TripoError, "animate_in_place"):
            self.cli("tripo_retarget", input="p-1", animations=["a"], animate_in_place=True)

    def test_convert_exports_in_the_requested_format(self):
        dest = Path(self.tmp.name) / "m.obj"
        self.cli("tripo_convert", input="p-1", format="OBJ", out=str(dest), pack_uv=True, texture_size=2048)
        call = self.call_for("export")
        self.assertEqual(call[call.index("--format") + 1], "obj")
        self.assertIn("--pack-uv=true", call)
        self.assertEqual(call[call.index("--texture-size") + 1], "2048")
        self.assertEqual(call[call.index("-o") + 1], str(dest))

    def test_convert_rejects_formats_the_cli_lacks(self):
        with self.assertRaisesRegex(tripo.TripoError, "exports glb"):
            self.cli("tripo_convert", input="p-1", format="GLTF")

    def test_task_shows_details(self):
        self.responses[("task", "get")] = (0, '{"status": "success"}\n')
        self.assertIn('"status": "success"', self.cli("tripo_task", task_id="t-1"))


class TestCliErrors(CliBase):
    def test_login_problem_says_how_to_sign_in(self):
        self.responses[("generate", "text")] = (1, "ERROR: session expired\n")
        with self.assertRaisesRegex(tripo.TripoError, "tripo-cli auth login"):
            self.cli("tripo_generate", prompt="x")

    def test_last_error_line_is_the_message_and_progress_is_dropped(self):
        self.responses[("generate", "text")] = (1, "Status: running (5%)\nERROR: not enough credits\n")
        with self.assertRaises(tripo.TripoError) as ctx:
            self.cli("tripo_generate", prompt="x")
        self.assertEqual(str(ctx.exception), "ERROR: not enough credits")

    def test_timeout(self):
        with mock.patch.object(tripo.subprocess, "run",
                               side_effect=tripo.subprocess.TimeoutExpired("fake-tripo", 1)):
            with self.assertRaisesRegex(tripo.TripoError, "did not finish"):
                self.cli("tripo_generate", prompt="x")


class TestCliImageAndBalance(CliBase):
    def test_image_is_fetched_from_the_asset_url_not_the_cli_output_flag(self):
        url = f"http://127.0.0.1:{self.server.server_port}/cdn/model.glb"
        self.responses[("image",)] = (0, "OK: Task created: img-1\n")
        self.responses[("image", "get")] = (0, json.dumps({"asset_id": "img-1", "url": url}))
        out = self.tool("tripo_image", prompt="expression sheet", scale="4:3", model="gpt_image_2",
                        out=self.tmp.name + os.sep)
        make = next(c for c in self.calls if c[0] == "image" and c[1] != "get")
        self.assertEqual(make[make.index("-m") + 1], "gpt_image_2")
        self.assertEqual(make[make.index("--scale") + 1], "4:3")
        self.assertNotIn("-o", make)
        self.assertEqual(make[-2:], ["--", "expression sheet"])
        self.assertEqual((Path(self.tmp.name) / "img-1.glb").read_bytes(), MODEL_BYTES)
        self.assertIn("balance:", out)

    def test_image_without_out_returns_the_url(self):
        self.responses[("image",)] = (0, "OK: Task created: img-1\n")
        self.responses[("image", "get")] = (0, json.dumps({"url": "https://cdn/x.png"}))
        self.assertIn("https://cdn/x.png", self.tool("tripo_image", prompt="x"))

    def test_image_input_must_exist(self):
        with self.assertRaisesRegex(tripo.TripoError, "local image files"):
            self.tool("tripo_image", prompt="x", input=str(Path(self.tmp.name) / "nope.png"))

    def test_image_without_a_task_is_an_error(self):
        self.responses[("image",)] = (0, "something odd\n")
        with self.assertRaisesRegex(tripo.TripoError, "did not report a task"):
            self.tool("tripo_image", prompt="x")

    def test_balance_on_the_cli(self):
        out = self.cli("tripo_balance")
        self.assertIn("5910 expire 2026-10-30", out)

    def test_balance_on_the_api(self):
        FakeTripo.requests = []
        with mock.patch.object(tripo, "call", return_value={"balance": 12}) as call:
            out = self.tool("tripo_balance", backend="api")
        call.assert_called_once_with("GET", "/account/balance")
        self.assertIn('"balance": 12', out)


class TestProtocol(Base):
    def rpc(self, *messages):
        """Run main() on newline-delimited JSON-RPC and return the replies."""
        stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
        stdout = io.StringIO()
        with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", stdout):
            tripo.main()
        return [json.loads(line) for line in stdout.getvalue().splitlines()]

    def test_initialize_list_and_notification(self):
        replies = self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(replies), 2)  # the notification gets no answer
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertEqual(names, set(tripo.BY_NAME))
        for t in replies[1]["result"]["tools"]:
            self.assertNotIn("run", t)
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_tool_error_is_a_result_not_a_protocol_error(self):
        (reply,) = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "tripo_generate", "arguments": {}}})
        self.assertTrue(reply["result"]["isError"])

    def test_unknown_tool_and_method(self):
        a, b = self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}},
            {"jsonrpc": "2.0", "id": 2, "method": "nope"})
        self.assertEqual(a["error"]["code"], -32601)
        self.assertEqual(b["error"]["code"], -32601)

    def test_end_to_end_call_over_the_wire(self):
        (reply,) = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "tripo_generate", "arguments": {"prompt": "x", "out": self.tmp.name + os.sep}}})
        self.assertFalse(reply["result"]["isError"])
        self.assertTrue((Path(self.tmp.name) / "task_1.glb").exists())

    def test_garbage_line_is_a_parse_error(self):
        with mock.patch("sys.stdin", io.StringIO("{not json\n")), mock.patch("sys.stdout", io.StringIO()) as out:
            tripo.main()
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)

    def test_every_tool_schema_names_real_required_fields(self):
        for t in tripo.TOOLS:
            props = t["inputSchema"].get("properties", {})
            for req in t["inputSchema"].get("required", []):
                self.assertIn(req, props, f"{t['name']}: required {req} not in properties")

    def test_no_em_dashes_in_the_source(self):
        text = (HERE / "tripo-mcp.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search("—", text))


if __name__ == "__main__":
    unittest.main()
