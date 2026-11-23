#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocsr_backends.py — OCSR 可插拔执行后端（BackendAdapter）与统一执行配置。

只封装与具体 CLI（opencode / codex）强耦合的 6 个薄职责：
1. validate_config       — 模型白名单与参数组合 fail-closed 校验
2. build_command_fragment — 注入通用 launcher 的可执行命令与 stdin 管道片段
3. session_id_field      — 流式事件提取 session_id 的顶层 JSON 字段名
4. parse_events          — 从原始 run.log JSONL 提取唯一 session ID 与阶段观测
5. build_resume_argv     — 构造 resume-material.json 的恢复命令参数
6. build_preflight_fragment — 构造 preflight 探针命令片段

外围看护（KILL_ON_JOB_CLOSE Job、看门狗、状态机、返工双计数、账本、面板）
保留在 ocsr_dispatch.py 通用层，不在后端中复制。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BACKEND_OPENCODE = "opencode"
BACKEND_CODEX = "codex"
SUPPORTED_BACKENDS = frozenset({BACKEND_OPENCODE, BACKEND_CODEX})

SANDBOX_READ_ONLY = "read-only"
SANDBOX_WORKSPACE_WRITE = "workspace-write"
SUPPORTED_CODEX_SANDBOXES = frozenset({SANDBOX_READ_ONLY, SANDBOX_WORKSPACE_WRITE})

ARTIFACT_MODE_WORKER_WRITE = "worker-write"
ARTIFACT_MODE_CAPTURE_REPLY = "capture-reply"
SUPPORTED_ARTIFACT_MODES = frozenset({
    ARTIFACT_MODE_WORKER_WRITE,
    ARTIFACT_MODE_CAPTURE_REPLY,
})

DISPATCH_META_FILE = "dispatch-meta.json"
DISPATCH_META_SCHEMA_VERSION = 1

_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")


def default_codex_allowed_models_path(ocsr_home: Path) -> Path:
    override = os.environ.get("OCSR_CODEX_ALLOWED_MODELS_PATH")
    if override:
        return Path(override)
    return ocsr_home / "allowed-codex-models.json"


def load_codex_allowed_models(path: Path) -> dict[str, dict[str, Any]]:
    """读取 ~/.ocsr/allowed-codex-models.json（结构化对象数组）。

    文件缺失、空白或空数组视为未配置（返回 {}）；
    非法 JSON、缺少必填字段或重复 model 一律 fail-closed 抛 RuntimeError。
    每项 schema：
      {"model": "gpt-6.1-sol", "provider": "openai", "family": "gpt",
       "reasoning_efforts": ["low", "medium", "high"]}  # reasoning_efforts 可选
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError(f"Cannot load OCSR Codex allowlist from {path}: {exc}") from exc
    if not text.strip():
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cannot load OCSR Codex allowlist from {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise RuntimeError(f"OCSR Codex allowlist at {path} must be a JSON array of objects.")

    result: dict[str, dict[str, Any]] = {}
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RuntimeError(f"OCSR Codex allowlist entry #{idx} at {path} must be an object.")
        model = item.get("model")
        provider = item.get("provider")
        family = item.get("family")
        if (
            not isinstance(model, str) or not model.strip() or model != model.strip()
            or "/" in model or not _SAFE_TOKEN_RE.fullmatch(model)
        ):
            raise RuntimeError(
                f"OCSR Codex allowlist entry #{idx} has invalid 'model': {model!r} "
                f"(must be a bare CLI model slug without '/')."
            )
        if not isinstance(provider, str) or not provider.strip() or provider != provider.strip():
            raise RuntimeError(
                f"OCSR Codex allowlist entry #{idx} ('{model}') missing valid 'provider'."
            )
        if not isinstance(family, str) or not family.strip() or family != family.strip():
            raise RuntimeError(
                f"OCSR Codex allowlist entry #{idx} ('{model}') missing valid 'family'."
            )
        efforts_raw = item.get("reasoning_efforts", [])
        if (
            not isinstance(efforts_raw, list)
            or any(not isinstance(e, str) or not e.strip() or e != e.strip() for e in efforts_raw)
            or len(efforts_raw) != len(set(efforts_raw))
        ):
            raise RuntimeError(
                f"OCSR Codex allowlist entry #{idx} ('{model}') has invalid 'reasoning_efforts'."
            )
        if model in result:
            raise RuntimeError(
                f"OCSR Codex allowlist at {path} contains duplicate model '{model}'."
            )
        result[model] = {
            "model": model,
            "provider": provider,
            "family": family,
            "reasoning_efforts": tuple(efforts_raw),
        }
    return result


@dataclass(frozen=True)
class ExecutionConfig:
    """所有入口（CLI / run --spec / dsh）经校验后收敛到的统一执行配置。"""

    backend: str
    cli_model: str
    provider: str
    family: str
    work_dir: str
    sandbox: str | None = None
    reasoning_effort: str | None = None
    artifact_mode: str = ARTIFACT_MODE_WORKER_WRITE
    config_source: str = ""

    def to_dispatch_meta(self) -> dict[str, Any]:
        return {
            "schema_version": DISPATCH_META_SCHEMA_VERSION,
            "backend": self.backend,
            "cli_model": self.cli_model,
            "provider": self.provider,
            "family": self.family,
            "work_dir": self.work_dir,
            "sandbox_requested": self.sandbox,
            "sandbox_effective": self.sandbox if self.backend == BACKEND_CODEX else None,
            "reasoning_effort_requested": self.reasoning_effort,
            "artifact_mode": self.artifact_mode,
            "config_source": self.config_source,
        }


def _path_is_within(child: Path, parent: Path) -> bool:
    try:
        child_res = child.resolve()
        parent_res = parent.resolve()
        child_res.relative_to(parent_res)
        return True
    except (ValueError, OSError):
        return False


def validate_execution_config(
    *,
    backend: str | None,
    model: str,
    work_dir: str,
    sandbox: str | None = None,
    reasoning_effort: str | None = None,
    artifact_mode: str | None = None,
    expected_output: Path | None = None,
    opencode_allowed_models: frozenset[str] | set[str] | tuple[str, ...] = (),
    codex_allowed_models: dict[str, dict[str, Any]] | None = None,
    opencode_unconfigured_msg: str = "OCSR opencode model allowlist is not configured.",
    codex_allowlist_path: Path | None = None,
) -> ExecutionConfig:
    """统一校验执行配置；任何未知后端、未配置白名单或非法组合均 fail-closed 抛 ValueError。"""
    resolved_backend = (backend or BACKEND_OPENCODE).strip()
    if resolved_backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"Unsupported backend '{resolved_backend}'. "
            f"Supported backends: {', '.join(sorted(SUPPORTED_BACKENDS))}."
        )
    if not isinstance(model, str) or not model.strip() or model != model.strip():
        raise ValueError(f"Invalid model identifier: {model!r}")

    if resolved_backend == BACKEND_OPENCODE:
        if sandbox is not None:
            raise ValueError(
                f"--sandbox '{sandbox}' is only supported by the 'codex' backend; "
                f"'opencode' does not provide a process-level sandbox."
            )
        if reasoning_effort is not None:
            raise ValueError(
                f"--reasoning-effort '{reasoning_effort}' is only supported by the 'codex' backend."
            )
        resolved_artifact_mode = artifact_mode or ARTIFACT_MODE_WORKER_WRITE
        if resolved_artifact_mode != ARTIFACT_MODE_WORKER_WRITE:
            raise ValueError(
                f"--artifact-mode '{resolved_artifact_mode}' is not supported by 'opencode' "
                f"(only '{ARTIFACT_MODE_WORKER_WRITE}' is supported)."
            )
        allowed_set = frozenset(opencode_allowed_models)
        if not allowed_set:
            raise ValueError(opencode_unconfigured_msg)
        if model not in allowed_set:
            allowed = ", ".join(sorted(allowed_set))
            raise ValueError(
                f"Model '{model}' is not in the OCSR allowlist.\n"
                f"Allowed models: {allowed}\n"
                f"Run 'opencode models --verbose' to check "
                f"whether any allowed model is configured locally."
            )
        provider = model.split("/", 1)[0] if "/" in model else "unknown"
        return ExecutionConfig(
            backend=BACKEND_OPENCODE,
            cli_model=model,
            provider=provider,
            family="",
            work_dir=work_dir,
            sandbox=None,
            reasoning_effort=None,
            artifact_mode=ARTIFACT_MODE_WORKER_WRITE,
            config_source="allowed-models.json",
        )

    # backend == BACKEND_CODEX
    if "/" in model:
        raise ValueError(
            f"Codex backend requires a bare CLI model slug (without '/'), got '{model}'. "
            f"Do not encode backend or provider as a model prefix."
        )
    codex_pool = codex_allowed_models or {}
    if not codex_pool:
        hint = str(codex_allowlist_path) if codex_allowlist_path else "~/.ocsr/allowed-codex-models.json"
        raise ValueError(
            f"OCSR Codex model allowlist is not configured (missing or empty file at {hint}).\n"
            f"Write verified model entries [{{'model','provider','family','reasoning_efforts'}}] to {hint}."
        )
    entry = codex_pool.get(model)
    if entry is None:
        allowed = ", ".join(sorted(codex_pool))
        raise ValueError(
            f"Codex model '{model}' is not in the OCSR Codex allowlist.\n"
            f"Allowed Codex models: {allowed}"
        )

    resolved_sandbox = sandbox or SANDBOX_WORKSPACE_WRITE
    if resolved_sandbox not in SUPPORTED_CODEX_SANDBOXES:
        raise ValueError(
            f"Invalid Codex --sandbox '{resolved_sandbox}'. "
            f"Allowed values: {', '.join(sorted(SUPPORTED_CODEX_SANDBOXES))}."
        )

    if artifact_mode is None:
        resolved_artifact_mode = (
            ARTIFACT_MODE_CAPTURE_REPLY
            if resolved_sandbox == SANDBOX_READ_ONLY
            else ARTIFACT_MODE_WORKER_WRITE
        )
    else:
        if artifact_mode not in SUPPORTED_ARTIFACT_MODES:
            raise ValueError(
                f"Invalid --artifact-mode '{artifact_mode}'. "
                f"Allowed values: {', '.join(sorted(SUPPORTED_ARTIFACT_MODES))}."
            )
        resolved_artifact_mode = artifact_mode

    if resolved_sandbox == SANDBOX_READ_ONLY and resolved_artifact_mode != ARTIFACT_MODE_CAPTURE_REPLY:
        raise ValueError(
            "Codex --sandbox read-only cannot write target files directly; "
            "use --artifact-mode capture-reply (default for read-only)."
        )

    if reasoning_effort is not None:
        supported_efforts = entry.get("reasoning_efforts", ())
        if reasoning_effort not in supported_efforts:
            allowed_eff = ", ".join(supported_efforts) if supported_efforts else "(none configured)"
            raise ValueError(
                f"Reasoning effort '{reasoning_effort}' is not allowed for Codex model '{model}'. "
                f"Configured reasoning_efforts: {allowed_eff}."
            )

    if (
        resolved_sandbox == SANDBOX_WORKSPACE_WRITE
        and resolved_artifact_mode == ARTIFACT_MODE_WORKER_WRITE
        and expected_output is not None
        and work_dir
    ):
        if not _path_is_within(expected_output, Path(work_dir)):
            raise ValueError(
                f"Codex workspace-write with worker-write requires expected output '{expected_output}' "
                f"to reside inside work_dir '{work_dir}', or use --artifact-mode capture-reply."
            )

    return ExecutionConfig(
        backend=BACKEND_CODEX,
        cli_model=model,
        provider=str(entry["provider"]),
        family=str(entry["family"]),
        work_dir=work_dir,
        sandbox=resolved_sandbox,
        reasoning_effort=reasoning_effort,
        artifact_mode=resolved_artifact_mode,
        config_source=str(codex_allowlist_path.name) if codex_allowlist_path else "allowed-codex-models.json",
    )


def write_dispatch_meta(wd: Path, cfg: ExecutionConfig) -> None:
    (wd / DISPATCH_META_FILE).write_text(
        json.dumps(cfg.to_dispatch_meta(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_dispatch_meta(wd: Path) -> dict[str, Any] | None:
    path = wd / DISPATCH_META_FILE
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _ps_single_quote(value: str) -> str:
    return value.replace("'", "''")


class BackendAdapter:
    """薄后端适配器基类。"""

    name: str = ""
    session_id_field: str = ""

    def extract_session_ids(self, events: list[dict[str, Any]]) -> tuple[set[str], int, dict[str, Any] | None]:
        ids: set[str] = set()
        event_count = 0
        last_event: dict[str, Any] | None = None
        field = self.session_id_field
        for event in events:
            event_count += 1
            last_event = event
            candidate = event.get(field)
            if isinstance(candidate, str) and candidate.strip():
                ids.add(candidate.strip())
        return ids, event_count, last_event

    def parse_phase(self, events: list[dict[str, Any]]) -> str:
        return "unknown"

    def build_command_fragment(
        self,
        cfg: ExecutionConfig,
        *,
        label: str,
        cwd_literal: str,
        resume_session: str = "",
        capture_tmp_expr: str = "",
    ) -> str:
        raise NotImplementedError

    def build_resume_argv(self, meta: dict[str, Any], session_id: str) -> list[str]:
        raise NotImplementedError

    def build_preflight_fragment(self, cfg: ExecutionConfig, *, cwd_literal: str) -> str:
        raise NotImplementedError


class OpenCodeAdapter(BackendAdapter):
    name = BACKEND_OPENCODE
    session_id_field = "sessionID"

    def build_command_fragment(
        self,
        cfg: ExecutionConfig,
        *,
        label: str,
        cwd_literal: str,
        resume_session: str = "",
        capture_tmp_expr: str = "",
    ) -> str:
        safe_label = re.sub(r"[^\w\u4e00-\u9fff-]", "_", str(label))[:30]
        session_arg = f"--session {resume_session} " if resume_session else ""
        model_arg = f'{session_arg}-m {cfg.cli_model} --title "ocsr-{safe_label}"'
        return f"& opencode run $prompt --format json --standalone {model_arg} '{cwd_literal}'"

    def build_resume_argv(self, meta: dict[str, Any], session_id: str) -> list[str]:
        return [
            "opencode", "run", "<RESUME_PROMPT>", "--format", "json", "--standalone",
            "--session", session_id, "-m", str(meta.get("cli_model") or meta.get("model") or ""),
            "--dir", str(meta.get("work_dir") or meta.get("execution_dir") or ""),
        ]

    def build_preflight_fragment(self, cfg: ExecutionConfig, *, cwd_literal: str) -> str:
        return f'& opencode run $prompt --format json --standalone -m {cfg.cli_model} --title "ocsr-preflight" \'{cwd_literal}\''


class CodexAdapter(BackendAdapter):
    name = BACKEND_CODEX
    session_id_field = "thread_id"

    def parse_phase(self, events: list[dict[str, Any]]) -> str:
        if not events:
            return "unknown"
        last = events[-1]
        etype = str(last.get("type") or "")
        if etype == "turn.completed":
            return "completed"
        if etype == "item.started":
            item = last.get("item") if isinstance(last.get("item"), dict) else {}
            if item.get("type") == "command_execution":
                return "tool_running"
            return "active"
        if etype == "item.completed":
            item = last.get("item") if isinstance(last.get("item"), dict) else {}
            if item.get("type") == "agent_message":
                return "assistant_message"
            if item.get("type") == "command_execution":
                return "tool_completed"
            return "active"
        if etype in ("thread.started", "turn.started"):
            return "started"
        return "unknown"

    def build_command_fragment(
        self,
        cfg: ExecutionConfig,
        *,
        label: str,
        cwd_literal: str,
        resume_session: str = "",
        capture_tmp_expr: str = "",
    ) -> str:
        sandbox = cfg.sandbox or SANDBOX_WORKSPACE_WRITE
        effort_part = (
            f" -c 'model_reasoning_effort=\"{cfg.reasoning_effort}\"'"
            if cfg.reasoning_effort
            else ""
        )
        out_part = f" --output-last-message {capture_tmp_expr}" if capture_tmp_expr else ""
        if resume_session:
            # Phase 0 实证：前置 --sandbox 真实生效，resume 使用当前工作目录（launcher 已 Set-Location）。
            return (
                f"$prompt | & codex exec --sandbox {sandbox} --skip-git-repo-check --color never "
                f"resume --json -m '{_ps_single_quote(cfg.cli_model)}'{effort_part}{out_part} "
                f"'{_ps_single_quote(resume_session)}' -"
            )
        return (
            f"$prompt | & codex exec --cd '{cwd_literal}' --sandbox {sandbox} "
            f"--skip-git-repo-check --color never --json "
            f"-m '{_ps_single_quote(cfg.cli_model)}'{effort_part}{out_part} -"
        )

    def build_resume_argv(self, meta: dict[str, Any], session_id: str) -> list[str]:
        sandbox = str(meta.get("sandbox_requested") or SANDBOX_WORKSPACE_WRITE)
        argv = [
            "codex", "exec", "--sandbox", sandbox, "--skip-git-repo-check",
            "--color", "never", "resume", "--json",
            "-m", str(meta.get("cli_model") or meta.get("model") or ""),
        ]
        effort = meta.get("reasoning_effort_requested")
        if effort:
            argv.extend(["-c", f'model_reasoning_effort="{effort}"'])
        argv.extend([session_id, "-"])
        return argv

    def build_preflight_fragment(self, cfg: ExecutionConfig, *, cwd_literal: str) -> str:
        effort_part = (
            f" -c 'model_reasoning_effort=\"{cfg.reasoning_effort}\"'"
            if cfg.reasoning_effort
            else ""
        )
        return (
            f"$prompt | & codex exec --cd '{cwd_literal}' --sandbox read-only "
            f"--skip-git-repo-check --color never --json "
            f"-m '{_ps_single_quote(cfg.cli_model)}'{effort_part} -"
        )


_ADAPTERS: dict[str, BackendAdapter] = {
    BACKEND_OPENCODE: OpenCodeAdapter(),
    BACKEND_CODEX: CodexAdapter(),
}


def get_backend_adapter(backend: str | None = None) -> BackendAdapter:
    key = (backend or BACKEND_OPENCODE).strip()
    adapter = _ADAPTERS.get(key)
    if adapter is None:
        raise ValueError(f"Unsupported backend: {key!r}")
    return adapter
