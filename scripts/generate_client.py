# -*- coding: utf-8 -*-
"""从 OpenAPI 规格生成前端 TypeScript 客户端（P1-6 契约优先）。

数据流（三段式）
--------------
::

    app/contracts/openapi.py  ──build_spec()──>  OpenAPI 3.1 字典
                                    │
    scripts/generate_client.py   ┘──>  frontend/src/api/generated/**
                                    │
    scripts/check_generated_client.py 在 CI 里比对该目录是否过期

**只对有真 schema 的 operation 生成类型化方法。**
从 ``url_map`` 反射来的那 200+ 条路由本轮只钉住了路径与方法，没有
response schema（协调书 R5：既有路由本轮只做纯搬迁）。给没有 schema 的
operation 生成 ``request<any>`` 只会制造「看起来有类型」的假安全感，
所以它们被显式跳过，只出现在导出的规格文件里供人查阅。

确定性（CI 门禁的前提）
----------------------
生成物**不含任何时间戳、不含随机 id、不依赖字典遍历顺序**。同样的规格
必然产出逐字节相同的文件，否则「生成物是否过期」这个检查每次都会误报。

安全边界
--------
* **不启动服务、不监听端口、不连 ComfyUI、不碰 GPU**；
* 默认**不 import app**（``app/app.py`` 拆分期间可能半完成，且 import 会
  调度 autopilot 托管恢复）。``--with-app`` 才会反射全量路由，且会强制
  ``MJSCXT_AUTOPILOT=0``。

用法::

    python scripts/generate_client.py                 # 写生成物
    python scripts/generate_client.py --out DIR       # 写到别处（比对用）
    python scripts/generate_client.py --print-spec    # 只打印规格
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))

from contracts import openapi as openapi_spec  # noqa: E402  必须在 sys.path 设置之后

_ROOT_DIR = _ROOT
DEFAULT_OUT = os.path.join(_ROOT, "frontend", "src", "api", "generated")

#: 生成物文件头。**每个生成文件都带**，且明确写「禁止手改」——
#: 手工改生成物的人下次跑一次生成就白干了，且 CI 会拦。
BANNER = """// ============================================================
// 本文件是**自动生成物**，禁止手改。
//
// 生成命令：python scripts/generate_client.py
// 契约来源：app/contracts/openapi.py → GET /api/contracts/openapi.json
// 校验门禁：python scripts/check_generated_client.py
//
// 手改这里没有任何意义：下一次生成会被逐字覆盖，且 CI 会因
// 「生成物已过期」而失败。要改接口，去改后端 + 规格，然后重新生成。
// ============================================================
"""

PRIMITIVES = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
}

#: TS 标识符规则。OpenAPI 允许 ``x-`` 前缀扩展键（如 ``x-contract-version``），
#: 它们不是合法标识符，**必须加引号**，否则生成的 types.ts 直接语法错误。
_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def _prop_key(name: str) -> str:
    """属性名 → TS 对象字面量键（必要时加引号）。"""
    return name if _IDENT.match(name) else "'{}'".format(name.replace("'", "\\'"))


# --------------------------------------------------------------------------
# JSON Schema → TypeScript
# --------------------------------------------------------------------------

def _ref_name(schema: Dict[str, Any]) -> str:
    ref = str(schema.get("$ref") or "")
    return ref.rsplit("/", 1)[-1] if ref else ""


def ts_type(schema: Optional[Dict[str, Any]]) -> str:
    """单个 schema → TS 类型表达式。

    刻意保守：读不懂的结构一律落 ``unknown``。生成一个「看起来对」的
    类型比生成 ``unknown`` 危险 —— 后者会在用错的地方编译报错。
    """
    if not schema or not isinstance(schema, dict):
        return "unknown"

    # 3.1 允许 type 数组（联合），例如 ["string", "null"]
    stype = schema.get("type")
    if isinstance(stype, list):
        return " | ".join(ts_type({**schema, "type": t}) for t in stype)
    if stype is None and "$ref" in schema:
        return _ref_name(schema)
    if "const" in schema:
        return _literal(schema["const"])
    if schema.get("enum"):
        return " | ".join(_literal(v) for v in schema["enum"])

    if stype == "array":
        return "{0}[]".format(ts_type(schema.get("items")))
    if stype == "object":
        ap = schema.get("additionalProperties")
        if isinstance(ap, dict):
            return "Record<string, {0}>".format(ts_type(ap))
        if schema.get("properties"):
            return "Record<string, unknown>"
        return "Record<string, unknown>"
    if stype in PRIMITIVES:
        return PRIMITIVES[stype]
    if "oneOf" in schema or "anyOf" in schema:
        variants = schema.get("oneOf") or schema.get("anyOf") or []
        return " | ".join(ts_type(v) for v in variants) or "unknown"
    return "unknown"


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return "'{}'".format(value.replace("\\", "\\\\").replace("'", "\\'"))
    return "unknown"


def _is_required(name: str, required: List[str]) -> bool:
    return name in (required or [])


def render_types(schemas: Dict[str, Dict[str, Any]]) -> str:
    """组件 schema → TS interface 声明。"""
    out: List[str] = [BANNER, "",
                      "/** 契约类型（由 OpenAPI components/schemas 生成） */", ""]
    for name in sorted(schemas):
        schema = schemas[name] or {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        description = str(schema.get("description") or "").strip()

        out.append("/**")
        if description:
            for line in description.splitlines():
                out.append(" * {}".format(line.strip()))
        out.append(" * @see components/schemas/{}".format(name))
        out.append(" */")
        out.append("export interface {} {{".format(name))
        if not props:
            out.append("  [key: string]: unknown;")
        for prop in sorted(props):
            doc = str((props[prop] or {}).get("description") or "").strip()
            if doc:
                for line in doc.splitlines():
                    out.append("  /** {} */".format(line.strip()))
            opt = "" if _is_required(prop, required) else "?"
            out.append("  {}{}: {};".format(_prop_key(prop), opt, ts_type(props[prop])))
        out.append("}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------

#: ``client.ts:37-40`` 的错误脱敏 —— 全仓最有价值的一段前端逻辑。
#:
#: 后端统一返回 ``{success, error?, message?}`` 且文案已脱敏。若这里退化成
#: ``HTTP 500: Internal Server Error``，用户看到的就是无信息的英文报错。
#: **迁移到生成客户端时不要丢**（协调书 R1/R2 的配套要求）。
READ_ERROR_TS = '''
// 后端统一返回 {success, error?, message?}。此前 request() 直接抛
// `HTTP 500: Internal Server Error`，把后端精心脱敏过的中文错误丢掉了，
// 界面上只能看到无信息的英文报错。这里优先取后端的可读文案。
async function readError(response: Response): Promise<string> {
  let detail = '';
  try {
    const data = await response.clone().json();
    const raw = data?.error || data?.message || data?.detail;
    if (typeof raw === 'string' && raw.trim()) detail = raw.trim();
    else if (raw) detail = JSON.stringify(raw);
    // 后端常带 `hint`（例如「这集可能是兜底生成的，没有台词」）或 `guide`
    // （例如视觉模型不适配的替代建议）。这些是给用户看的处置办法，
    // 只把 error 抛出去会让用户看到问题却不知道怎么办。
    const extra = data?.hint || data?.guide || data?.layout_hint;
    if (typeof extra === 'string' && extra.trim()) {
      detail = detail ? `${detail}（${extra.trim()}）` : extra.trim();
    }
  } catch {
    try {
      const text = (await response.text()).trim();
      if (text) detail = text;
    } catch {
      /* 响应体不可读，退化为状态码 */
    }
  }
  const status = `HTTP ${response.status}`;
  return detail ? `${detail}` : `${status} ${response.statusText || ''}`.trim();
}
'''


def _method_name(tag: str, operation_id: str) -> str:
    """operationId → 方法名（camelCase 兜底：非法字符转下划线）。"""
    raw = operation_id or ""
    return raw[0].lower() + raw[1:] if raw else "operation"


def _render_operation(path: str, method: str, op: Dict[str, Any]) -> str:
    """单个 operation → 一段 TS 方法代码。"""
    op_id = str(op.get("operationId") or "")
    name = _method_name(op["tags"][0] if op.get("tags") else "api", op_id)
    summary = str(op.get("summary") or "")

    args: List[str] = []
    query_names: List[str] = []

    # 路径参数：在模板字符串里就地替换 `{name}`
    #
    # ⚠️ 必须剥掉 OpenAPI 路径里的 '/api' 前缀：生成的 request() 内部是
    # `fetch(`${API_BASE}${path}`)` 且 API_BASE === '/api'。不剥就会拼成
    # '/api/api/contracts/version' → 404，契约闸门横幅恒红、生成客户端整体不可用
    # （2026-10-07 实测：ApiCompatibilityGate 一直显示 contract.unavailable）。
    # 下方注释里展示的仍是真实完整路径，便于人对着规格查。
    _API_BASE = "/api"
    if path == _API_BASE:
        path_literal = "/"
    elif path.startswith(_API_BASE + "/"):
        path_literal = path[len(_API_BASE):]
    else:
        path_literal = path
    for param in op.get("parameters") or []:
        if param.get("in") != "path":
            continue
        pname = param["name"]
        args.append("{}: {}".format(pname, ts_type(param.get("schema"))))
        path_literal = path_literal.replace(
            "{{{}}}".format(pname),
            "${{encodeURIComponent(String({}))}}".format(pname))

    # 查询参数：收集成 qs({...}) 调用
    for param in op.get("parameters") or []:
        if param.get("in") != "query":
            continue
        pname = param["name"]
        opt = "" if param.get("required") else "?"
        args.append("{}{}: {}".format(pname, opt, ts_type(param.get("schema"))))
        query_names.append(pname)

    # 注意闭合反引号必须放在 qs() **之后**：写成 `` `/path`${qs(..)} `` 是语法错误
    if query_names:
        path_expr = "`{}${{qs({{ {} }})}}`".format(
            path_literal, ", ".join(query_names))
    else:
        path_expr = "`{}`".format(path_literal)

    init: List[str] = ["method: '{}'".format(method.upper())]
    body = ((op.get("requestBody") or {}).get("content") or {}).get("application/json")
    if body:
        body_schema = body.get("schema") or {}
        body_required = bool((op.get("requestBody") or {}).get("required", True))
        args.append("{}: {}".format("body" if body_required else "body?", ts_type(body_schema)))
        init.append("body: JSON.stringify(body)")

    ret = _ok_response_type(op)
    lines: List[str] = []
    lines.append("  /**")
    if summary:
        lines.append("   * {}".format(summary))
    lines.append("   *")
    lines.append("   * `{} {}`".format(method.upper(), path))
    lines.append("   */")
    lines.append("  {}: ({}) =>".format(name, ", ".join(args) if args else ""))
    lines.append("    request<{}>({}, {{ {} }}),".format(ret, path_expr, ", ".join(init)))
    return "\n".join(lines)


def _ok_response_type(op: Dict[str, Any]) -> str:
    """200 响应 schema → 返回类型。

    生成的类型刻意**不覆盖** ``success`` 字段：既有前端把
    ``request<{success: boolean; project: Project}>`` 这类结构直接当返回值用，
    生成的类型要能与之共存，所以这里返回**整个响应对象**的类型。
    """
    content = ((op.get("responses") or {}).get("200") or {}).get("content") or {}
    schema = (content.get("application/json") or {}).get("schema") or {}
    return ts_type(schema)


def _tag_of(op: Dict[str, Any]) -> str:
    tags = op.get("tags") or []
    return str(tags[0]) if tags else "misc"


def _tag_api_name(tag: str) -> str:
    return "{}Api".format(tag.replace("-", "").replace("_", ""))


def render_client(paths: Dict[str, Dict[str, Any]], schemas: Dict[str, Dict[str, Any]],
                  contract_version: str, spec_hash: str) -> str:
    """生成客户端 ``client.ts``。"""
    out: List[str] = [BANNER]
    out.append("import type {")
    for name in sorted(schemas):
        out.append("  {},".format(name))
    out.append("} from './types';")
    out.append("")
    out.append("const API_BASE = '/api';")
    out.append("")
    out.append(READ_ERROR_TS)
    out.append("""
async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  // FormData 必须让浏览器自行生成 multipart boundary —— 一旦手工带上
  // Content-Type: application/json，boundary 就没了，后端 request.files 收到空列表。
  const isForm = typeof FormData !== 'undefined' && options.body instanceof FormData;
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: isForm
      ? { ...options.headers }
      : { 'Content-Type': 'application/json', ...options.headers },
  });
  if (!response.ok) {
    throw new Error(await readError(response));
  }
  return response.json() as Promise<T>;
}

/** 查询串拼装：跳过 undefined / null / 空串，避免出现 `?project=` 这种脏 URL */
function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join('&')}` : '';
}
""")

    # 按 tag 分组，只收 manual（有真 schema）的 operation
    by_tag: Dict[str, List[Tuple[str, str, Dict[str, Any]]]] = {}
    for path in sorted(paths):
        for method in sorted(paths[path] or {}):
            op = (paths[path] or {})[method] or {}
            if op.get("x-contract-source") != "manual":
                continue
            by_tag.setdefault(_tag_of(op), []).append((path, method, op))

    for tag in sorted(by_tag):
        out.append("")
        out.append("/**")
        out.append(" * {} —— 契约版本 {}（spec {}…）".format(
            tag, contract_version, spec_hash[:12]))
        out.append(" */")
        out.append("export const {} = {{".format(_tag_api_name(tag)))
        for path, method, op in by_tag[tag]:
            out.append(_render_operation(path, method, op))
        out.append("};")
    out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_index(contract_version: str, spec_hash: str, api_names: List[str],
                 type_names: List[str]) -> str:
    """生成 barrel 导出 ``index.ts``。

    **导出名必须去重**：``api_names`` 是**按 operation** 收集的，同一个 tag
    下有 N 个 operation 就会进 N 次同名导出。不去重会稳定产出 15 处
    ``TS2300 Duplicate identifier``（2026-10-07 实测），而新鲜度门禁照样
    PASS —— 它只校验「生成物 == 规格」，不校验生成物是否合法。
    """
    out = [BANNER, ""]
    out.append("export * from './types';")
    out.append("export {")
    for name in sorted(set(api_names)):
        out.append("  {},".format(name))
    out.append("} from './client';")
    out.append("")
    out.append("/** 契约版本与规格摘要 —— 前端启动闸门拿它判断客户端是否过期 */")
    out.append("export const CONTRACT_VERSION = '{}';".format(contract_version))
    out.append("export const SPEC_HASH = '{}';".format(spec_hash))
    out.append("")
    out.append("/** 生成物内可用的类型名（供泛型标注与测试引用） */")
    out.append("export type GeneratedTypeName =")
    for idx, name in enumerate(sorted(type_names)):
        tail = ";" if idx == len(sorted(type_names)) - 1 else ""
        out.append("  | '{}'{}".format(name, tail))
    out.append("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------

def build_spec(flask_app=None) -> Dict[str, Any]:
    """生成用的规格。默认只含手工 schema 部分（确定性，不依赖 app 是否可导入）。"""
    return openapi_spec.build_spec(flask_app, include_reflected=flask_app is not None)


def render_all(spec: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """规格 → ``{相对文件名: 文件内容}``。

    **纯函数**：不写盘、不打印、不带时间戳。``check_generated_client.py``
    直接拿它与磁盘内容逐字比对。
    """
    doc = spec if spec is not None else build_spec(None)
    paths = doc.get("paths") or {}
    schemas = (doc.get("components") or {}).get("schemas") or {}
    version = str(doc.get("x-contract-version") or openapi_spec.CONTRACT_VERSION)
    digest = openapi_spec.spec_hash(doc)

    api_names: List[str] = []
    for path in sorted(paths):
        for method in sorted(paths[path] or {}):
            op = (paths[path] or {})[method] or {}
            if op.get("x-contract-source") == "manual":
                api_names.append(_tag_api_name(_tag_of(op)))

    return {
        "types.ts": render_types(schemas),
        "client.ts": render_client(paths, schemas, version, digest),
        "index.ts": render_index(version, digest, api_names, list(schemas)),
        "openapi.snapshot.json": json.dumps(
            doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        "manifest.json": json.dumps({
            "contract_version": version,
            "spec_hash": digest,
            "openapi": doc.get("openapi"),
            "schema_count": len(schemas),
            "manual_path_count": len(doc.get("x-manual-paths") or []),
            "generated_by": "scripts/generate_client.py",
            "note": "本文件是自动生成物，禁止手改；无时间戳以保证生成可复现。",
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    }


def write_all(out_dir: str, files: Dict[str, str]) -> List[str]:
    """把生成物写到磁盘，返回写入的文件路径。"""
    os.makedirs(out_dir, exist_ok=True)
    written: List[str] = []
    for name in sorted(files):
        target = os.path.join(out_dir, name)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(files[name])
        written.append(target)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="从 OpenAPI 规格生成前端 TS 客户端")
    ap.add_argument("--out", default=DEFAULT_OUT, help="生成物输出目录")
    ap.add_argument("--with-app", action="store_true",
                    help="反射 Flask 全量路由（会 import app，必须设 MJSCXT_AUTOPILOT=0）")
    ap.add_argument("--print-spec", action="store_true", help="只打印规格 JSON，不写文件")
    ap.add_argument("--no-write", action="store_true", help="只渲染不落盘（自检用）")
    args = ap.parse_args()

    flask_app = None
    if args.with_app:
        # 必须在 import app 之前设：模块末尾会调度 autopilot 托管恢复
        os.environ.setdefault("MJSCXT_AUTOPILOT", "0")
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        import app as app_mod  # noqa: PLC0415
        flask_app = app_mod.app

    spec = build_spec(flask_app)
    if args.print_spec:
        print(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    files = render_all(spec)
    if args.no_write:
        print("已渲染 {} 个生成物（未落盘）".format(len(files)))
        return 0
    for path in write_all(args.out, files):
        print("已生成 {}".format(os.path.relpath(path, _ROOT_DIR).replace("\\", "/")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
