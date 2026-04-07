#!/usr/bin/env python3
"""
Патч для LiteLLM: поддержка Anthropic OAuth токенов (sk-ant-oat-*) в pass-through
и исправление проброса лишних полей в body.

Правка 1 — llm_passthrough_endpoints.py (anthropic_proxy_route):
  - Если клиент шлёт Authorization: Bearer sk-ant-oat-* -> используем OAuth токен клиента
  - Иначе -> старое поведение (серверный ANTHROPIC_API_KEY)

Правка 2 — litellm/passthrough/utils.py (forward_headers_from_request):
  - Стрипаем authorization, x-api-key, x-litellm-api-key из request_headers
    перед мержем с custom_headers, чтобы не было конфликта

Правка 3 — litellm/proxy/pass_through_endpoints/pass_through_endpoints.py
  (_init_kwargs_for_pass_through_endpoint):
  - Стрипаем extra_headers из body перед отправкой на upstream.
    Claude Code (Anthropic JS SDK) кладёт extra_headers в JSON body,
    Anthropic API отвергает его как неизвестное поле:
    "extra_headers: Extra inputs are not permitted"

Применяется при сборке Docker образа через werf.
"""

import glob
import os
import sys

PASSTHROUGH_ENDPOINTS_PATHS = [
    "/app/litellm/proxy/pass_through_endpoints/llm_passthrough_endpoints.py",
    "/usr/lib/python3.13/site-packages/litellm/proxy/pass_through_endpoints/llm_passthrough_endpoints.py",
]

PASSTHROUGH_UTILS_PATHS = [
    "/app/litellm/passthrough/utils.py",
    "/usr/lib/python3.13/site-packages/litellm/passthrough/utils.py",
]

PASS_THROUGH_ENDPOINTS_PATHS = [
    "/app/litellm/proxy/pass_through_endpoints/pass_through_endpoints.py",
    "/usr/lib/python3.13/site-packages/litellm/proxy/pass_through_endpoints/pass_through_endpoints.py",
]

PATCH_MARKER_1 = "# anthropic OAuth passthrough by alfa-leasing patch"
PATCH_MARKER_2 = "# strip auth headers by alfa-leasing patch"
PATCH_MARKER_3 = "# strip extra_headers from body by alfa-leasing patch"

# =============================================================================
# Правка 1: import + OAuth-aware auth в anthropic_proxy_route
# =============================================================================

OLD_IMPORT = "from litellm.llms.anthropic.common_utils import AnthropicModelInfo"
NEW_IMPORT = (
    "from litellm.llms.anthropic.common_utils import AnthropicModelInfo, "
    "is_anthropic_oauth_key  " + PATCH_MARKER_1
)

# Два варианта OLD — для разных версий LiteLLM.
# >= 1.83: get_auth_header() возвращает dict с правильным ключом
# <= 1.82: прямая конструкция {"x-api-key": ...}

OLD_PASSTHROUGH_BLOCK_NEW = """\
    ## CREATE PASS-THROUGH
    auth_header = AnthropicModelInfo.get_auth_header(anthropic_api_key or None)
    endpoint_func = create_pass_through_route(
        endpoint=endpoint,
        target=str(updated_url),
        custom_headers=auth_header if auth_header is not None else {},
        _forward_headers=True,
        is_streaming_request=is_streaming_request,
    )  # dynamically construct pass-through endpoint based on incoming path"""

OLD_PASSTHROUGH_BLOCK_LEGACY = """\
    ## CREATE PASS-THROUGH
    endpoint_func = create_pass_through_route(
        endpoint=endpoint,
        target=str(updated_url),
        custom_headers={"x-api-key": "{}".format(anthropic_api_key)},
        _forward_headers=True,
        is_streaming_request=is_streaming_request,
    )  # dynamically construct pass-through endpoint based on incoming path"""

NEW_PASSTHROUGH_BLOCK = """\
    ## CREATE PASS-THROUGH
    """ + PATCH_MARKER_1 + """
    # Check if client sends an OAuth token (sk-ant-oat-*) in Authorization header
    _request_headers = _safe_get_request_headers(request)
    _client_auth = _request_headers.get("authorization", "")
    if is_anthropic_oauth_key(_client_auth):
        # OAuth flow: use client's token, don't add server api_key
        _pt_custom_headers = {
            "authorization": _client_auth,
            "anthropic-dangerous-direct-browser-access": "true",
        }
    else:
        # Standard flow: use server's ANTHROPIC_API_KEY
        auth_header = AnthropicModelInfo.get_auth_header(anthropic_api_key or None)
        _pt_custom_headers = auth_header if auth_header is not None else {}

    endpoint_func = create_pass_through_route(
        endpoint=endpoint,
        target=str(updated_url),
        custom_headers=_pt_custom_headers,
        _forward_headers=True,
        is_streaming_request=is_streaming_request,
    )  # dynamically construct pass-through endpoint based on incoming path"""

# =============================================================================
# Правка 2: strip auth из request_headers
# =============================================================================

OLD_FORWARD_HEADERS = """\
        if forward_headers is True:
            # Header We Should NOT forward
            request_headers.pop("content-length", None)
            request_headers.pop("host", None)

            # Combine request headers with custom headers
            headers = {**request_headers, **headers}"""

NEW_FORWARD_HEADERS = """\
        if forward_headers is True:
            # Headers we should NOT forward to upstream
            request_headers.pop("content-length", None)
            request_headers.pop("host", None)
            """ + PATCH_MARKER_2 + """
            # Auth headers are handled via custom_headers -- strip from request
            # to avoid conflicts (e.g. server x-api-key vs client OAuth token)
            request_headers.pop("authorization", None)
            request_headers.pop("x-api-key", None)
            # LiteLLM proxy auth -- must never leak to upstream
            request_headers.pop("x-litellm-api-key", None)

            # Combine request headers with custom headers
            headers = {**request_headers, **headers}"""

# =============================================================================
# Правка 3: strip extra_headers из JSON body
# =============================================================================

# Claude Code (Anthropic JS SDK) sends extra_headers in JSON body.
# Anthropic API rejects it: "extra_headers: Extra inputs are not permitted"
# LiteLLM's _init_kwargs pops all_litellm_params from body, but extra_headers
# is not in that list, so it leaks through to upstream.

OLD_INIT_KWARGS_BODY_STRIP = """\
        litellm_params_in_body = {}
        for k in all_litellm_params:
            if k in _parsed_body:
                litellm_params_in_body[k] = _parsed_body.pop(k, None)"""

NEW_INIT_KWARGS_BODY_STRIP = """\
        litellm_params_in_body = {}
        for k in all_litellm_params:
            if k in _parsed_body:
                litellm_params_in_body[k] = _parsed_body.pop(k, None)
        """ + PATCH_MARKER_3 + """
        # SDK clients (e.g. Claude Code / Anthropic JS SDK) may put extra_headers
        # in JSON body; upstream APIs reject unknown fields.
        _parsed_body.pop("extra_headers", None)"""


# =============================================================================
# Patch functions
# =============================================================================

def invalidate_pyc_cache(file_path):
    """Удалить .pyc кеш чтобы Python перекомпилировал пропатченный файл."""
    pycache_dir = os.path.join(os.path.dirname(file_path), "__pycache__")
    basename = os.path.splitext(os.path.basename(file_path))[0]
    for pyc in glob.glob(os.path.join(pycache_dir, f"{basename}*.pyc")):
        os.remove(pyc)
        print(f"  Removed stale .pyc: {pyc}")


def patch_passthrough_endpoints(file_path):
    """Правка 1: OAuth-aware auth selection в anthropic_proxy_route."""
    print(f"\nPatching: {file_path}")

    if not os.path.exists(file_path):
        print("  Skipped (file not found)")
        return False

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    if PATCH_MARKER_1 in content:
        print("  Patch already applied, skipping...")
        return True

    # Шаг 1: добавить import is_anthropic_oauth_key
    if OLD_IMPORT in content:
        content = content.replace(OLD_IMPORT, NEW_IMPORT, 1)
        print("  + Patched import: added is_anthropic_oauth_key")
    else:
        print("  ERROR: Could not find import pattern")
        print(f"  Looking for: {repr(OLD_IMPORT)}")
        return False

    # Шаг 2: заменить логику выбора auth (пробуем оба варианта)
    if OLD_PASSTHROUGH_BLOCK_NEW in content:
        content = content.replace(OLD_PASSTHROUGH_BLOCK_NEW, NEW_PASSTHROUGH_BLOCK, 1)
        print("  + Patched anthropic_proxy_route (matched >= 1.83 pattern)")
    elif OLD_PASSTHROUGH_BLOCK_LEGACY in content:
        content = content.replace(
            OLD_PASSTHROUGH_BLOCK_LEGACY, NEW_PASSTHROUGH_BLOCK, 1
        )
        print("  + Patched anthropic_proxy_route (matched legacy pattern)")
    else:
        print("  ERROR: Could not find pass-through block pattern")
        print("  Neither >= 1.83 nor legacy pattern matched.")
        print("  Hint: check the exact code around '## CREATE PASS-THROUGH' comment")
        return False

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    invalidate_pyc_cache(file_path)
    print("  Patch applied successfully!")
    return True


def patch_passthrough_utils(file_path):
    """Правка 2: strip auth headers перед мержем в forward_headers_from_request."""
    print(f"\nPatching: {file_path}")

    if not os.path.exists(file_path):
        print("  Skipped (file not found)")
        return False

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    if PATCH_MARKER_2 in content:
        print("  Patch already applied, skipping...")
        return True

    if OLD_FORWARD_HEADERS not in content:
        print("  ERROR: Could not find forward_headers pattern")
        print(f"  Looking for: {repr(OLD_FORWARD_HEADERS[:80])}")
        return False

    content = content.replace(OLD_FORWARD_HEADERS, NEW_FORWARD_HEADERS, 1)
    print("  + Patched forward_headers_from_request: strip auth headers")

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    invalidate_pyc_cache(file_path)
    print("  Patch applied successfully!")
    return True


def patch_pass_through_body_strip(file_path):
    """Правка 3: strip extra_headers из body перед отправкой на upstream."""
    print(f"\nPatching: {file_path}")

    if not os.path.exists(file_path):
        print("  Skipped (file not found)")
        return False

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    if PATCH_MARKER_3 in content:
        print("  Patch already applied, skipping...")
        return True

    if OLD_INIT_KWARGS_BODY_STRIP not in content:
        print("  ERROR: Could not find _init_kwargs body strip pattern")
        print(f"  Looking for: {repr(OLD_INIT_KWARGS_BODY_STRIP[:80])}")
        return False

    content = content.replace(
        OLD_INIT_KWARGS_BODY_STRIP, NEW_INIT_KWARGS_BODY_STRIP, 1
    )
    print("  + Patched _init_kwargs: strip extra_headers from body")

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    invalidate_pyc_cache(file_path)
    print("  Patch applied successfully!")
    return True


def apply_patch():
    patched_1 = False
    patched_2 = False
    patched_3 = False

    print(
        "\n=== Patch 1: Anthropic OAuth passthrough (llm_passthrough_endpoints.py) ==="
    )
    for path in PASSTHROUGH_ENDPOINTS_PATHS:
        if patch_passthrough_endpoints(path):
            patched_1 = True

    print(
        "\n=== Patch 2: Strip auth headers in forward_headers (passthrough/utils.py) ==="
    )
    for path in PASSTHROUGH_UTILS_PATHS:
        if patch_passthrough_utils(path):
            patched_2 = True

    print(
        "\n=== Patch 3: Strip extra_headers from body (pass_through_endpoints.py) ==="
    )
    for path in PASS_THROUGH_ENDPOINTS_PATHS:
        if patch_pass_through_body_strip(path):
            patched_3 = True

    return patched_1 and patched_2 and patched_3


if __name__ == "__main__":
    print("Applying Anthropic OAuth passthrough patch to LiteLLM...")
    success = apply_patch()
    if success:
        print("\nAll patches applied successfully!")
    else:
        print("\nFailed to apply some patches")
    sys.exit(0 if success else 1)
