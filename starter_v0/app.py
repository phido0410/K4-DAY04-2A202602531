import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

# Setup paths and environment
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ARTIFACTS_DIR = ROOT / "artifacts"
TRANSCRIPTS_DIR = ROOT / "transcripts"
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

from chat import (
    assistant_tool_message,
    execute_tool_call,
    json_text,
    now_iso,
    run_model_tool_loop,
    safe_slug,
    tool_results_message,
    trim_history,
    write_transcript,
)
from env_loader import load_lab_env
from providers import make_provider
from tools import load_tool_declarations, to_openai_tools
from versioning import artifact_version_dict, build_artifact_version

# Load environment variables
load_lab_env(ROOT)

st.set_page_config(
    page_title="IT Helpdesk Agent Live Chat",
    page_icon="🛠️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for polished aesthetic
st.markdown(
    """
    <style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #1E293B;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        font-size: 1rem;
        color: #64748B;
        margin-bottom: 1.5rem;
    }
    .badge {
        display: inline-block;
        padding: 0.25rem 0.6rem;
        font-size: 0.8rem;
        font-weight: 600;
        border-radius: 9999px;
        background-color: #E2E8F0;
        color: #0F172A;
        margin-right: 0.5rem;
    }
    .tool-badge {
        background-color: #DBEAFE;
        color: #1E40AF;
        border: 1px solid #BFDBFE;
    }
    .version-badge {
        background-color: #DCFCE7;
        color: #166534;
        border: 1px solid #BBF7D0;
    }
    .alert-ticket-created {
        padding: 0.75rem 1rem;
        border-radius: 0.5rem;
        background-color: #ECFDF5;
        border: 1px solid #A7F3D0;
        color: #065F46;
        margin: 0.5rem 0;
    }
    .alert-ticket-forged {
        padding: 0.75rem 1rem;
        border-radius: 0.5rem;
        background-color: #FEF2F2;
        border: 1px solid #FECACA;
        color: #991B1B;
        margin: 0.5rem 0;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def parse_assistant_response(
    raw_text: str | None,
    status: str,
    tool_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Parse assistant text according to JSON contract (v6-v8/v10) and verify ticket creation safety."""
    cleaned = (raw_text or "").strip()

    # 1. Verify actual ticket creation in tool execution results
    actual_ticket_created = False
    created_ticket_info: dict[str, Any] | None = None
    for event in tool_events:
        if event.get("tool") == "create_ticket":
            res = event.get("result", {})
            if isinstance(res, dict) and res.get("status") == "created":
                actual_ticket_created = True
                created_ticket_info = res

    # 2. If status is waiting_for_user (clarify called), display as plain text
    if status == "waiting_for_user":
        return {
            "display_text": cleaned,
            "json_payload": None,
            "actual_ticket_created": False,
            "created_ticket_info": None,
            "ticket_hallucinated": False,
            "is_clarify": True,
        }

    # 3. Attempt to parse JSON contract:
    # Contract: {"intent": ..., "action": ..., "reply": ..., "evidence_ids": ...}
    parsed_json: dict[str, Any] | None = None

    # Try full json.loads
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            parsed_json = data
    except Exception:
        pass

    # Try extracting from ```json ... ``` markdown block
    if not parsed_json:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                if isinstance(data, dict):
                    parsed_json = data
            except Exception:
                pass

    # Try extracting outermost { ... }
    if not parsed_json:
        first_brace = cleaned.find("{")
        last_brace = cleaned.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            try:
                data = json.loads(cleaned[first_brace : last_brace + 1])
                if isinstance(data, dict):
                    parsed_json = data
            except Exception:
                pass

    # Determine display text
    if parsed_json and isinstance(parsed_json, dict):
        display_text = parsed_json.get("reply") or cleaned
    else:
        display_text = cleaned

    # Check for forged / hallucinated ticket action (action: created_ticket without tool execution)
    agent_claimed_ticket = False
    if parsed_json and isinstance(parsed_json, dict):
        if parsed_json.get("action") in ("created_ticket", "create_ticket"):
            agent_claimed_ticket = True

    # Catch claims of ticket creation in Vietnamese or English across display_text and cleaned
    ticket_creation_patterns = [
        r"đã\s+tạo\s+ticket",
        r"ticket\s+đã\s+được\s+tạo",
        r"ticket\s+được\s+tạo",
        r"tạo\s+ticket\s+thành\s+công",
        r"khởi\s+tạo\s+ticket\s+thành\s+công",
        r"đã\s+khởi\s+tạo\s+ticket",
        r"ticket\s+này\s+đã\s+được\s+tạo",
        r"ticket\s+đã\s+tạo",
        r"created\s+ticket",
        r"ticket\s+(?:has\s+been\s+)?created",
    ]
    if not agent_claimed_ticket:
        text_corpus = f"{cleaned} {display_text}"
        for pattern in ticket_creation_patterns:
            if re.search(pattern, text_corpus, re.IGNORECASE):
                agent_claimed_ticket = True
                break

    ticket_hallucinated = agent_claimed_ticket and not actual_ticket_created

    return {
        "display_text": display_text,
        "json_payload": parsed_json,
        "actual_ticket_created": actual_ticket_created,
        "created_ticket_info": created_ticket_info,
        "ticket_hallucinated": ticket_hallucinated,
        "is_clarify": False,
    }


# ----------------- SIDEBAR CONFIGURATION -----------------
with st.sidebar:
    st.title("⚙️ Cấu hình Agent")
    st.caption("Role D — Live Chat Interface & Inspector")

    provider_name = st.selectbox(
        "Provider",
        options=["openai", "openrouter", "gemini", "anthropic"],
        index=0,
        help="Chọn nhà cung cấp mô hình LLM",
    )

    default_models = {
        "openai": "gpt-4o-mini",
        "openrouter": "openai/gpt-4o-mini",
        "gemini": "gemini-3.5-flash",
        "anthropic": "claude-haiku-4-5-20251001",
    }
    default_model = default_models.get(provider_name, "")

    model_override = st.text_input(
        "Model (để trống dùng default)",
        value="",
        placeholder=default_model,
        help=f"Mặc định trong code là {default_model}",
    )
    active_model = model_override.strip() or default_model

    # Check API key
    key_env_map = {
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }
    required_env_key = key_env_map.get(provider_name, "")
    current_key_val = os.getenv(required_env_key, "")

    if current_key_val:
        st.success(f"🔑 Đã tìm thấy `{required_env_key}` trong .env", icon="✅")
    else:
        st.warning(f"⚠️ Chưa có `{required_env_key}` trong môi trường!", icon="⚠️")
        custom_key = st.text_input(f"Nhập {required_env_key}", type="password")
        if custom_key:
            os.environ[required_env_key] = custom_key.strip()
            st.rerun()

    st.divider()

    version_label = st.selectbox(
        "Artifact Version",
        options=["v10", "v5", "v0", "v1", "v2", "v3", "v9"],
        index=0,
        help="Phiên bản thử nghiệm (v10: hiện hành của nhóm)",
    )

    system_prompt_path = st.text_input(
        "System Prompt Path",
        value=str(ARTIFACTS_DIR / "system_prompt.md"),
    )
    tools_path = st.text_input(
        "Tools Declarations Path",
        value=str(ARTIFACTS_DIR / "tools.yaml"),
    )

    history_window = st.slider("History Window (pairs)", min_value=1, max_value=10, value=5)
    max_tool_rounds = st.slider("Max Tool Rounds", min_value=1, max_value=8, value=4)

    prompt_file = Path(system_prompt_path)
    tools_file = Path(tools_path)

    if prompt_file.exists() and tools_file.exists():
        artifact_ver = build_artifact_version(version_label, prompt_file, tools_file)
        st.markdown(f"**Artifact Version Hash:**")
        st.code(artifact_ver.artifact_version, language="text")
    else:
        st.error("Không tìm thấy file prompt hoặc tools!")
        artifact_ver = None

    st.divider()

    if st.button("🔄 Đặt lại Chat", use_container_width=True):
        st.session_state.messages = []
        st.session_state.history = []
        st.session_state.transcript = None
        st.rerun()

# ----------------- SESSION STATE INITIALIZATION -----------------
if "messages" not in st.session_state:
    st.session_state.messages = []

if "history" not in st.session_state:
    st.session_state.history = []

if "transcript" not in st.session_state or st.session_state.transcript is None:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    transcript_id = "_".join([
        safe_slug(version_label),
        safe_slug(provider_name),
        timestamp,
    ])
    transcript_path = TRANSCRIPTS_DIR / f"{transcript_id}.transcript.json"
    st.session_state.transcript_path = transcript_path
    st.session_state.transcript = {
        "transcript_id": transcript_id,
        **(artifact_version_dict(artifact_ver) if artifact_ver else {}),
        "provider": provider_name,
        "model": active_model,
        "system_prompt": str(system_prompt_path),
        "tools": str(tools_path),
        "history_window": history_window,
        "max_tool_rounds": max_tool_rounds,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "turns": [],
    }
else:
    # Dynamically update transcript metadata if user changes settings/artifact in sidebar
    if artifact_ver:
        st.session_state.transcript.update(artifact_version_dict(artifact_ver))
    st.session_state.transcript["provider"] = provider_name
    st.session_state.transcript["model"] = active_model
    st.session_state.transcript["system_prompt"] = str(system_prompt_path)
    st.session_state.transcript["tools"] = str(tools_path)
    st.session_state.transcript["history_window"] = history_window
    st.session_state.transcript["max_tool_rounds"] = max_tool_rounds

# ----------------- MAIN UI -----------------
st.markdown('<div class="main-header">🛠️ IT Helpdesk Agent Live Chat</div>', unsafe_allow_html=True)
st.markdown(
    f"""
    <div>
        <span class="badge version-badge">Version: {artifact_ver.artifact_version if artifact_ver else version_label}</span>
        <span class="badge tool-badge">Provider: {provider_name} ({active_model})</span>
    </div>
    <div class="sub-header">Giao diện kiểm thử trực quan cấu trúc Tool Calling và phản hồi của Agent theo chuẩn Day 04 Lab.</div>
    """,
    unsafe_allow_html=True,
)

# Quick Demo Preset Scenarios
with st.expander("💡 Gợi ý kịch bản Demo nhanh (Click để tham khảo)", expanded=False):
    st.markdown(
        """
        - **1. Happy path (VPN Status):** `Kiểm tra trạng thái dịch vụ VPN production giúp mình.`
        - **2. Missing info (Yêu cầu làm rõ):** `Máy tính của tôi đang bị hỏng, bạn kiểm tra giúp tôi với.`
        - **3. Multi-turn Correction:** `À nhầm, máy tính của mình là LT-240 chứ không phải LT-204, kiểm tra lại giúp.`
        - **4. Action boundary (Tạo ticket an toàn):** `Tạo ticket thay pin cho laptop LT-411 mức medium giúp mình.`
        """
    )


def render_assistant_turn(msg: dict[str, Any]) -> None:
    # 1. Tool execution rounds
    rounds = msg.get("rounds", [])
    if rounds:
        for rnd in rounds:
            calls = rnd.get("tool_calls", [])
            results = rnd.get("tool_results", [])
            if calls:
                with st.expander(f"⚙️ Vòng gọi tool #{rnd.get('round', 1)}: {len(calls)} công cụ được gọi", expanded=False):
                    for idx, call in enumerate(calls):
                        st.markdown(f"**Tool:** `{call.get('name')}`")
                        st.json(call.get("args", {}))
                        if idx < len(results):
                            res = results[idx]
                            st.markdown("**Kết quả thực thi:**")
                            st.json(res.get("result", {}))
                            if "error" in res.get("result", {}):
                                st.error(f"Error: {res['result'].get('error')} - {res['result'].get('message')}")

    parsed = msg.get("parsed")
    if parsed:
        # 2. Safety ticket alerts
        if parsed.get("actual_ticket_created"):
            ticket_info = parsed.get("created_ticket_info", {})
            st.markdown(
                f"""
                <div class="alert-ticket-created">
                    🎫 <b>Xác thực hành động ghi:</b> Ticket đã được tạo thành công trong hệ thống!<br>
                    • Mã Ticket: <code>{ticket_info.get('ticket_id', 'N/A')}</code><br>
                    • File: <code>{ticket_info.get('path', 'N/A')}</code>
                </div>
                """,
                unsafe_allow_html=True,
            )
        elif parsed.get("ticket_hallucinated"):
            st.markdown(
                """
                <div class="alert-ticket-forged">
                    🛡️ <b>CẢNH BÁO AN TOÀN (False Confirmation):</b> Agent thông báo đã tạo ticket nhưng 
                    <b>KHÔNG CÓ</b> lệnh <code>create_ticket</code> nào trả về <code>status: created</code> trong hệ thống.
                    Không có ticket nào được ghi vào ổ đĩa.
                </div>
                """,
                unsafe_allow_html=True,
            )

        # 3. Contract JSON expander if parsed
        json_payload = parsed.get("json_payload")
        if json_payload:
            with st.expander(f"📋 Contract JSON Output (Intent: {json_payload.get('intent', 'N/A')} | Action: {json_payload.get('action', 'N/A')})", expanded=False):
                st.json(json_payload)

        # 4. Main message content
        st.markdown(parsed.get("display_text", ""))
    else:
        st.markdown(msg.get("content", ""))


# Render Chat History
for msg in st.session_state.messages:
    role = msg["role"]
    with st.chat_message(role):
        if role == "assistant":
            render_assistant_turn(msg)
        else:
            st.markdown(msg["content"])

# User Chat Input
if prompt := st.chat_input("Nhập yêu cầu cần trợ giúp IT..."):
    # Render user message
    st.chat_message("user").markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    if not prompt_file.exists() or not tools_file.exists():
        st.error("Không tìm thấy file prompt hoặc tools để chạy!")
        st.stop()

    system_prompt = prompt_file.read_text(encoding="utf-8")
    tool_declarations = load_tool_declarations(tools_file)
    openai_tools = to_openai_tools(tool_declarations)

    try:
        provider = make_provider(provider_name)
    except Exception as exc:
        st.error(f"Lỗi khởi tạo provider: {exc}")
        st.stop()

    messages = [
        {"role": "system", "content": system_prompt},
        *trim_history(st.session_state.history, history_window),
        {"role": "user", "content": prompt},
    ]

    # Dynamically update transcript metadata to reflect current active settings
    if artifact_ver:
        st.session_state.transcript.update(artifact_version_dict(artifact_ver))
    st.session_state.transcript["provider"] = provider_name
    st.session_state.transcript["model"] = active_model
    st.session_state.transcript["system_prompt"] = str(system_prompt_path)
    st.session_state.transcript["tools"] = str(tools_path)
    st.session_state.transcript["history_window"] = history_window
    st.session_state.transcript["max_tool_rounds"] = max_tool_rounds
    st.session_state.transcript["updated_at"] = now_iso()

    turn_index = len(st.session_state.transcript.get("turns", [])) + 1
    turn_record: dict[str, Any] = {
        "turn_index": turn_index,
        "started_at": now_iso(),
        "user": prompt,
        "status": "started",
        "assistant_text": None,
        "rounds": [],
        "tool_events": [],
        "artifact_version": artifact_ver.artifact_version if artifact_ver else version_label,
    }

    with st.chat_message("assistant"):
        with st.spinner("Đang suy luận và chọn tool..."):
            try:
                result = run_model_tool_loop(
                    provider=provider,
                    messages=messages,
                    tools=openai_tools,
                    model=model_override.strip() or None,
                    max_tool_rounds=max_tool_rounds,
                )
                turn_record.update(result)
                assistant_text = result["assistant_text"]

                # Parse contract and ticket creation
                parsed = parse_assistant_response(
                    raw_text=assistant_text,
                    status=result.get("status", "answered"),
                    tool_events=result.get("tool_events", []),
                )

                msg_obj = {
                    "role": "assistant",
                    "content": assistant_text,
                    "rounds": result.get("rounds", []),
                    "parsed": parsed,
                }
                render_assistant_turn(msg_obj)

                st.session_state.history.append({"role": "user", "content": prompt})
                st.session_state.history.append({"role": "assistant", "content": assistant_text})
                st.session_state.messages.append(msg_obj)

            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {str(exc)}"
                turn_record.update({
                    "status": "provider_error",
                    "error": error_msg,
                })
                st.error(f"Provider Error: {error_msg}")
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": f"⚠️ Đã xảy ra lỗi: {error_msg}",
                    "rounds": [],
                    "parsed": None,
                })

            turn_record["ended_at"] = now_iso()
            st.session_state.transcript["turns"].append(turn_record)
            write_transcript(st.session_state.transcript_path, st.session_state.transcript)

# Sidebar Transcript Download
with st.sidebar:
    st.divider()
    st.subheader("📑 Lưu trữ Transcript")
    st.caption(f"File log: `{st.session_state.transcript_path.name}`")
    st.download_button(
        label="📥 Tải Transcript JSON",
        data=json.dumps(st.session_state.transcript, ensure_ascii=False, indent=2, default=str),
        file_name=st.session_state.transcript_path.name,
        mime="application/json",
        use_container_width=True,
    )
