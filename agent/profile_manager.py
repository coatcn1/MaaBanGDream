from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:
    from .realtime.profile_store import EnvironmentSignature, RealtimeProfileStore
except ImportError:
    from realtime.profile_store import EnvironmentSignature, RealtimeProfileStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _public(profile: dict[str, Any], signature: EnvironmentSignature | None) -> dict[str, Any]:
    result = {key: value for key, value in profile.items() if key != "_path"}
    result["filename"] = profile["_path"].name
    if signature is None:
        result["environment_match"] = None
    else:
        try:
            saved = EnvironmentSignature.from_mapping(profile.get("environment", {}))
            result["environment_match"] = (
                RealtimeProfileStore._same_non_speed_environment(saved, signature)
            )
        except ValueError:
            result["environment_match"] = False
    return result


def handle_request(request: dict[str, Any], *, root: str | Path = PROJECT_ROOT / "profiles") -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("请求必须是 JSON 对象")
    store = RealtimeProfileStore(root)
    operation = request.get("operation")
    if operation == "pin":
        return {"pinned": store.pin(str(request.get("difficulty", "")), str(request.get("profile", "")))}
    if operation == "unpin":
        return {"pinned": store.unpin(str(request.get("difficulty", "")))}
    if operation == "update-settings":
        settings = request.get("settings")
        if not isinstance(settings, dict):
            raise ValueError("settings 必须是 JSON 对象")
        updated = store.update_settings(str(request.get("profile", "")), **settings)
        return {"profile": _public(updated, None), "pinned": store._read_selection()}
    if operation == "update-runtime-options":
        options = request.get("runtime_options")
        if not isinstance(options, dict):
            raise ValueError("runtime_options 必须是 JSON 对象")
        return {"runtime_options": store.update_runtime_options(options)}
    if operation != "list":
        raise ValueError(f"不支持的操作: {operation!r}")

    difficulty = str(request.get("difficulty", ""))
    compatible = store.compatible_difficulties(difficulty)
    environment = request.get("environment")
    signature = EnvironmentSignature.from_mapping(environment) if isinstance(environment, dict) else None
    pinned = store._read_selection()
    selection: dict[str, Any] = {"mode": "pinned" if difficulty in pinned else "auto", "profile": pinned.get(difficulty), "source_difficulty": None}
    if signature is not None:
        try:
            selected = store.resolve_latest_for_environment(
                difficulty=difficulty,
                current_signature=signature,
            )
            selection["profile"] = selected.profile_path.name
            selection["source_difficulty"] = store.load(selected.profile_path.name).get("difficulty")
        except ValueError as exc:
            selection["error"] = str(exc)
    return {
        "difficulty": difficulty, "compatible_difficulties": list(compatible),
        "pinned": pinned, "selection": selection,
        "runtime_options": store.runtime_options(),
        "profiles": [_public(profile, signature) for profile in store.list_profiles()],
    }


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        response, code = {"ok": True, "result": handle_request(request)}, 0
    except (OSError, ValueError, TypeError) as exc:
        response, code = {"ok": False, "error": str(exc)}, 1
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
