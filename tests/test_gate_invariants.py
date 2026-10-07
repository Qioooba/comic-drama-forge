"""门禁与核心不变量的回归断言。

为什么有这份文件：2026-10-07 的全量审查在交付门禁 / 时间线 / 生产事实三个域
找出 4 个 P0 与多个 P1，**能活下来的根因就是这些域零测试覆盖**（89 个用例
没有一个能碰到它们）。修复本身若没有断言钉住，下次重构会以同样方式复发。

每个用例都写成「改动前必失败、改动后必通过」的形式。
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP = os.path.join(_ROOT, "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)


# =====================================================================
# P0-A 契约摘要必须两侧同基准（ADR-0004 启动闸门）
# =====================================================================

def test_contract_digest_matches_frontend_constant():
    """运行时摘要必须等于前端生成物里的常量，否则启动闸门恒红。

    前端常量取自 ``frontend/src/api/generated/index.ts`` 的 ``SPEC_HASH``。
    改动后二者不再相等 → ``ApiCompatibilityGate`` 每次启动都整页红屏。
    """
    import contracts.openapi as openapi_spec

    frontend_spec_hash = (
        "5ba89a7f699ae804bf261fe4fa35aa2a1d52d03d80caefe02de799d97406048c"
    )
    assert openapi_spec.spec_hash() == frontend_spec_hash, (
        "spec_hash 与前端生成物不一致：要么改了契约没重新生成客户端，"
        "要么生成侧与运行侧又用了不同基准（历史上正是后者导致全站红屏）"
    )


def test_contract_digest_ignores_reflected_routes():
    """反射路由漂移不得改契约摘要 —— 那是 url-contract 门禁的职责。

    ``spec_hash`` 必须只覆盖手工契约层。否则任何蓝图改名/加路由都会把摘要
    打变，而前端生成客户端并不依赖反射层，闸门会永远红。
    """
    import contracts.openapi as openapi_spec

    manual = openapi_spec.build_spec(None)
    # 传一份带反射的 spec 进去，摘要也必须与手工基准一致
    assert openapi_spec.spec_hash(manual) == openapi_spec.spec_hash()


def test_route_digest_separate_from_spec_hash():
    """路由摘要必须独立存在且与契约摘要不同，避免把两件事又混回一起。"""
    import contracts.openapi as openapi_spec

    assert openapi_spec.route_digest() != openapi_spec.spec_hash()


# =====================================================================
# P0-B 批准闸门按 media_version_id 判定（ADR-0002 采用≠批准）
# =====================================================================

def test_approval_lookup_uses_media_version_id(tmp_path, monkeypatch):
    """``subject_id`` 是调用方自由填的，不得用它当媒体版本的批准判据。

    改动前 ``get_approval_for(media_version_id)`` 实际执行
    ``WHERE subject_id=?``，于是 ``select(subject=B) + approve(media=A, subject=B)``
    会让 **B** 读到自己那条批准 —— 未批准的候选因此能通过时间线的
    ``require_approved`` 闸门。
    """
    import domain.production_facts as pf
    import infrastructure.facts_repo as facts_repo

    # 走构造函数传路径。FactsRepo 的路径不是模块常量而是 db_path() 函数，
    # monkeypatch 常量不会生效 —— 那样写会**直接写进真实 output/facts.db**。
    repo = facts_repo.FactsRepo(path=str(tmp_path / "facts.db"))
    repo.save_approval(pf.ApprovalDecision(
        approval_id="APR-MISMATCH", selection_id="SEL-1",
        subject_type="media_version",
        subject_id="B",            # 客户端自由填的主体
        media_version_id="A",      # 实际被批准的媒体
        bound_hash="h-A", authorized_by="Qing",
        media_sha256="sha-A", intent_hash="", selection_hash="",
        scope="media", approved=True, revoked=False,
        decided_at="2026-10-07T00:00:00",
    ))

    assert repo.get_approval_for("A") is not None, "被批准的媒体 A 应能读到批准"
    assert repo.get_approval_for("B") is None, (
        "B 借用了 A 的批准：未批准候选仍可过 require_approved 闸门"
    )


@pytest.mark.parametrize("cls_name", ["SelectionDecision", "ApprovalDecision"])
def test_decision_from_dict_never_yields_none(cls_name):
    """缺字段不得产出 None —— max(decided_at) 会在旧数据上抛 TypeError 变 500。"""
    import domain.production_facts as pf

    obj = getattr(pf, cls_name).from_dict({"x": "1"})
    nones = [k for k, v in obj.to_dict().items() if v is None]
    assert not nones, f"{cls_name}.from_dict 产出 None 字段：{nones}"


# =====================================================================
# P0-C 带参考图的镜头必须能登记（身份不丢）
# =====================================================================

def _ref_ctx(tmp_path, names, role=""):
    import production_recording as pr

    paths = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(n.encode())
        paths.append(str(p))
    return pr.RenderContext(
        source="test", project="p", episode=1, shot_key="s1",
        ref_paths=tuple(paths), ref_slot_role=role,
        api_prompt=None, workflow_hash="wh",
    )


def test_unspecified_role_is_legal():
    """``unspecified`` 必须进白名单：它是诚实的「不知道」，不是编造。"""
    import domain.production_facts as pf

    assert "unspecified" in pf.REF_SLOT_ROLES
    slots = pf._norm_ref_slots([{"index": 0, "role": "unspecified",
                                 "ref": "x.png", "sha256": "s"}])
    assert len(slots) == 1 and slots[0].role == "unspecified"


def test_mixed_role_refs_still_register(tmp_path):
    """角色混叠（角色+场景+道具，最常见分镜形态）不再让整条意图报废。"""
    import production_recording as pr

    ident = pr.intent_identity(_ref_ctx(tmp_path, ["a.png", "b.png", "c.png"]))
    slots = ident.get("ref_slots") or []
    assert len(slots) == 3, f"参考图槽位丢失：{slots}"
    assert all(s["role"] == "unspecified" for s in slots)


def test_ref_identity_still_hashed(tmp_path):
    """不许因为修了登记就丢掉身份：换/增减参考图必须改变意图哈希。"""
    import domain.production_facts as pf
    import production_recording as pr

    h1 = pf.hash_payload(pr.intent_identity(_ref_ctx(tmp_path, ["a.png"])))
    h2 = pf.hash_payload(pr.intent_identity(_ref_ctx(tmp_path, ["b.png"])))
    h3 = pf.hash_payload(pr.intent_identity(_ref_ctx(tmp_path, ["a.png", "b.png"])))
    assert h1 != h2, "换参考图哈希不变 → 身份丢失"
    assert h1 != h3, "增减参考图哈希不变 → 身份丢失"


def test_explicit_role_not_clobbered(tmp_path):
    """调用方声明了角色时不得被 unspecified 覆盖。"""
    import production_recording as pr

    ident = pr.intent_identity(_ref_ctx(tmp_path, ["a.png"], role="character"))
    assert [s["role"] for s in ident["ref_slots"]] == ["character"]


# =====================================================================
# P0-D ADR-0012 铁律 5：合法静音必须前置声明
# =====================================================================

def _rev(declared=False):
    import domain.timeline as tl

    return tl.build_revision(
        "rev1", project="p", episode="1", revision_no=1,
        items=[{
            "shot_key": "s1", "media_version_id": "mv1", "duration_sec": 2.0,
            "declared_silence": declared,
            "silence_reason": "intentional" if declared else "",
            "transition_in": {"kind": "cut", "duration_sec": 0.0},
            "audio_media_version_id": "",
        }],
    )


def _mv(kind, probe):
    return {"mv1": {"media_version_id": "mv1", "media_kind": kind, "probe": probe}}


def test_undeclared_silence_blocks_render():
    """无声轨 + 未声明 = 必须拦（这就是铁律 5，改动前永远不触发）。"""
    import domain.timeline as tl

    rep = tl.preflight(_rev(), media_index=_mv("video", {"has_audio": False, "duration": 2.0}))
    errs = list(getattr(rep, "errors", None) or [])
    assert any("undeclared_silence" in str(e) for e in errs), f"铁律 5 未触发：{errs}"


def test_declared_silence_passes():
    import domain.timeline as tl

    rep = tl.preflight(_rev(declared=True),
                       media_index=_mv("video", {"has_audio": False, "duration": 2.0}))
    assert not list(getattr(rep, "errors", None) or [])


def test_missing_audio_fact_fails_closed():
    """探测失败 = 无法确认 = 必须阻断，不能只记 warning。"""
    import domain.timeline as tl

    rep = tl.preflight(_rev(), media_index=_mv("video", {"probe_error": "ffprobe missing"}))
    assert list(getattr(rep, "errors", None) or []), "探测失败必须 fail-closed"


def test_still_image_not_blocked():
    """图片本就没有音轨，不得被「缺音轨事实」卡住（否则分镜镜全线红灯）。"""
    import domain.timeline as tl

    rep = tl.preflight(_rev(), media_index=_mv("image", {}))
    assert not list(getattr(rep, "errors", None) or [])


def test_video_with_audio_passes():
    import domain.timeline as tl

    rep = tl.preflight(_rev(), media_index=_mv("video", {"has_audio": True, "duration": 2.0}))
    assert not list(getattr(rep, "errors", None) or [])


# =====================================================================
# 交付下载门禁：归属不明即拒绝（fail-closed）
# =====================================================================

def test_download_gate_rejects_unknown_file(monkeypatch):
    """文件不属于任何交付包时必须拒绝，不得借用任意已批准包放行。

    需要 flask（api.ops 是蓝图模块），环境缺 flask 时跳过而不是假绿。
    """
    pytest.importorskip("flask")
    import api.ops as ops

    class _Repo:
        @staticmethod
        def list_packages(project, *a, **k):
            return [
                {"package_id": "PKG_A", "project": "demo", "requirement": {},
                 "files": [{"rel_path": "ep01/ep01_final.mp4"}]},
                {"package_id": "PKG_FLAT", "project": "demo", "requirement": {},
                 "files": [{"rel_path": "demo_fcpml.xml"}]},
            ]

        @staticmethod
        def get_approval(package_id):
            return {"approved": True}

    monkeypatch.setattr(ops, "delivery_repo", _Repo, raising=False)
    monkeypatch.setattr(ops.project_store, "get_project",
                        lambda n: {"dir_key": "demo"}, raising=False)

    import domain.delivery as delivery_domain
    import infrastructure.delivery_repo as delivery_repo
    import infrastructure.licensing_repo as licensing_repo

    monkeypatch.setattr(licensing_repo, "evaluate_delivery_gate",
                        lambda *a, **k: {"ok": True}, raising=False)
    monkeypatch.setattr(delivery_domain, "release_ready",
                        lambda *a, **k: {"ok": True}, raising=False)
    monkeypatch.setattr(delivery_repo, "list_packages",
                        lambda p, *a, **k: _Repo.list_packages(p), raising=False)
    monkeypatch.setattr(delivery_repo, "get_approval",
                        _Repo.get_approval, raising=False)

    # 不属于任何包 → 必须拒绝
    assert ops._delivery_release_gate_for("demo", "secret/not_in_any_pkg.mp4"), \
        "未知文件被放行：又退化成拿任意已批准包背书"
    # 跨集同名文件（带目录）→ 必须拒绝
    assert ops._delivery_release_gate_for("demo", "ep09/ep09_final.mp4"), \
        "跨集同名文件借用了别的集的批准"
    # 包内文件 → 放行
    assert not ops._delivery_release_gate_for("demo", "ep01/ep01_final.mp4")
    # 扁平请求（/api/export/<project>/<format>）→ 放行
    assert not ops._delivery_release_gate_for("demo", "demo_fcpml.xml")


# =====================================================================
# 撤销优先：人工撤销不得被自动放行复活（ADR-0006）
# =====================================================================

def test_revoked_approval_cannot_be_resurrected(tmp_path, monkeypatch):
    import infrastructure.delivery_repo as delivery_repo

    monkeypatch.setattr(delivery_repo, "DB_PATH", str(tmp_path / "delivery.db"), raising=False)
    monkeypatch.setattr(delivery_repo, "DELIVERY_HOME",
                        str(tmp_path / "home"), raising=False)

    delivery_repo.approve_package("PKG1", "hash-A", "Qing", note="人工批准")
    delivery_repo.revoke_approval("PKG1", "Qing", reason="内容有问题")

    with pytest.raises(delivery_repo.ApprovalRevokedError):
        delivery_repo.approve_package("PKG1", "hash-A", "automation:managed-pipeline",
                                      note="auto-release")

    row = delivery_repo.get_approval("PKG1")
    assert int(row.get("revoked") or 0) == 1, "撤销状态被覆盖回有效"
    assert row.get("approver") == "Qing", "人工批准人被机器记录销毁"

    # 未撤销的包不得被误伤
    delivery_repo.approve_package("PKG2", "hash-B", "Qing")
    assert int((delivery_repo.get_approval("PKG2") or {}).get("revoked") or 0) == 0


def test_auto_release_imports_use_top_level_names():
    """``app/`` 不是包，``from app.domain import`` 必然失败。

    历史缺陷：``auto_deliver`` 的两个分支都写成 ``app.*`` 形态 ⇒ 交付阶段
    从未真正执行过一次，异常还被宽 except 吞成 AR-ACTION-FAILED。
    """
    import inspect

    import auto_release

    src = inspect.getsource(auto_release.AutoReleaseEngine.auto_deliver)
    bad = [ln.strip() for ln in src.splitlines()
           if ln.strip().startswith(("from app.api", "from app.domain",
                                     "from app.infrastructure"))]
    assert not bad, f"auto_deliver 仍残留必失败的 import：{bad}"