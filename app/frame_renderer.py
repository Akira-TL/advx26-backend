from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .job_repository import ClaimedJob
from .object_store import FileSystemObjectStore
from .processing_worker import TerminalProcessingError, TransientProcessingError


class HeadlessFrameRenderer:
    """Invoke the production explicit-frame WebGL renderer through Node/Puppeteer."""

    def __init__(
        self,
        *,
        object_store: FileSystemObjectStore,
        project_dir: Path,
        node_binary: str = "node",
        timeout_seconds: float = 180,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.object_store = object_store
        self.project_dir = Path(project_dir).resolve()
        self.node_binary = node_binary
        self.timeout_seconds = timeout_seconds
        self._checked = False

    def check(self) -> None:
        if self._checked:
            return
        script = self.project_dir / "scripts" / "render-explicit-frames.mjs"
        render_page = self.project_dir / "dist" / "render.html"
        if not script.is_file() or not render_page.is_file():
            raise RuntimeError("renderer assets are not built")
        self._run([self.node_binary, "--version"], timeout_seconds=10)
        self._run(
            [
                self.node_binary,
                "--input-type=module",
                "-e",
                (
                    "import puppeteer from 'puppeteer';"
                    "const b=await puppeteer.launch({headless:true,args:["
                    "'--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',"
                    "'--enable-webgl','--ignore-gpu-blocklist','--use-gl=angle',"
                    "'--use-angle=swiftshader']});await b.close();"
                ),
            ],
            timeout_seconds=30,
        )
        self._checked = True

    def render(
        self,
        job: ClaimedJob,
        *,
        timeline_key: str,
        seed: int,
        output_dir: Path,
    ) -> dict[str, object]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=False)
        timeline_path = output_dir.parent / "timeline.json"
        try:
            with self.object_store.open_staging(timeline_key) as source:
                timeline_path.write_bytes(source.read())
        except OSError as error:
            raise TransientProcessingError(
                "FEATURE_TIMELINE_MISSING",
                "可视化特征时间线暂不可用",
            ) from error

        script = self.project_dir / "scripts" / "render-explicit-frames.mjs"
        dist = self.project_dir / "dist"
        try:
            self._run(
                [
                    self.node_binary,
                    str(script),
                    "--timeline",
                    str(timeline_path),
                    "--output",
                    str(output_dir),
                    "--dist",
                    str(dist),
                    "--seed",
                    str(seed),
                    "--width",
                    "480",
                    "--height",
                    "320",
                    "--timeout-ms",
                    str(round(self.timeout_seconds * 1000)),
                ],
                timeout_seconds=self.timeout_seconds + 10,
            )
        except FileNotFoundError as error:
            raise TransientProcessingError(
                "RENDERER_UNAVAILABLE",
                "可视化渲染器暂不可用",
            ) from error
        except subprocess.TimeoutExpired as error:
            raise TransientProcessingError(
                "RENDERER_TIMEOUT",
                "可视化渲染超时",
            ) from error
        except subprocess.CalledProcessError as error:
            raise TransientProcessingError(
                "RENDERER_FAILED",
                "可视化渲染暂时失败",
            ) from error

        try:
            summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
            if (
                not isinstance(summary, dict)
                or summary.get("width") != 480
                or summary.get("height") != 320
                or summary.get("frame_rate") != 10
                or not isinstance(summary.get("frames"), list)
                or summary.get("frame_count") != len(summary["frames"])
            ):
                raise ValueError("invalid renderer summary")
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise TerminalProcessingError(
                "RENDERER_OUTPUT_INVALID",
                "可视化帧输出无效",
            ) from error
        return summary

    def _run(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                args,
                cwd=self.project_dir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
            )
        except FileNotFoundError:
            raise
        except subprocess.TimeoutExpired:
            raise
        except subprocess.CalledProcessError:
            raise
