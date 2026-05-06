from __future__ import annotations

import json
from typing import Awaitable, Callable

from .artifact_parser import RoutedAction, detect_agentic_action
from .config import Settings


async def run_in_e2b(code: str, settings: Settings, lang: str = "python") -> str:
    if not settings.e2b_api_key:
        return "E2B indisponivel: `E2B_API_KEY` nao configurada."
    try:
        from e2b import Sandbox
    except Exception as exc:  # pragma: no cover - dependency/runtime guard
        return f"E2B indisponivel: sdk ausente ({type(exc).__name__})."

    sandbox = Sandbox.create(timeout=300, envs={"E2B_API_KEY": settings.e2b_api_key})
    try:
        if lang.lower() == "javascript":
            wrapped = "node <<'JS'\n" + code + "\nJS"
        else:
            wrapped = "python <<'PY'\n" + code + "\nPY"
        result = sandbox.commands.run(wrapped, timeout=90)
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        combined = "\n".join(part.strip() for part in [stdout, stderr] if part and part.strip()).strip()
        return combined or "Sandbox executado sem saida textual."
    finally:
        sandbox.kill()


async def mock_cloud_call(tool: str, payload: str) -> str:
    try:
        parsed = json.loads(payload)
    except Exception:
        parsed = {"raw": payload}
    return f"Cloud mock `{tool}` executado com payload: {json.dumps(parsed, ensure_ascii=False)}"


async def agentic_loop(
    prompt: str,
    settings: Settings,
    llm_call: Callable[[str], Awaitable[str]],
) -> str:
    conversation = [f"<user_request>{prompt}</user_request>"]
    while True:
        llm_response = await llm_call("\n\n".join(conversation))
        action = detect_agentic_action(llm_response)
        if not action:
            return llm_response.strip()
        if action.kind == "final_answer":
            return action.payload
        if action.kind == "execute_sandbox":
            result = await run_in_e2b(action.payload, settings, lang=action.language)
            conversation.append(llm_response)
            conversation.append(f"<tool_result>{result}</tool_result>")
            continue
        if action.kind == "execute_cloud":
            result = await mock_cloud_call(action.tool, action.payload)
            conversation.append(llm_response)
            conversation.append(f"<tool_result>{result}</tool_result>")
            continue
        return llm_response.strip()
