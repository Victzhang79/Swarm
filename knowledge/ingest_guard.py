"""知识库入库准入闸——三条入库通道的【单一事实源】。

背景（2026-07-29 深扫，26 号文 S-3）：Swarm 自己的 `.env`（含 5 类当时有效的凭据）
被向量化进了 Qdrant 共享集合。根因不是某一处写错，而是**准入面压根不存在**：

  - `EXCLUDED_EXTENSIONS` 是纯二进制/媒体清单，**无任何安全语义**；`.env` 的
    `Path(".env").suffix == ""`，从这个洞直穿；`id_rsa`/`server.pem`/`credentials.json`
    /`.npmrc`/`.netrc` 同理。
  - 只挡隐藏【目录】（`os.walk` 里 `d.startswith(".")`），不挡隐藏【文件】。
  - `.gitignore` 完全不被解析（全仓无库无依赖无实现），于是 `logs/`、`cassettes/`、
    轮转日志这些**项目自己早已声明忽略**的内容占了向量库 86.3%。
  - 三条通道（全量 preprocess / 增量 updater / 一致性 consistency）各有一套或干脆没有
    过滤：`updater` 不 import 任何排除表，比全量还宽；于是每日 04:00 的一致性修复会把
    全量通道挡掉的隐藏文件**重新塞回去**——准入面倒挂。

本模块把准入判据收敛到一处，职责分两层（**刻意不混同**）：

  层1·安全（fail-closed，绝不依赖外部工具）：敏感文件名 + 入库前内容密钥扫描。
      判不出/工具不可用时**拒绝入库**——凭据入库不可逆（向量库无认证、可被检索召回
      拼进 prompt 发往外部 LLM），宁可少索引一个文件。
  层2·降噪（fail-open）：`.gitignore` 遵守。git 不可用/非仓库时**放行**——它只影响
      索引质量不影响安全，硬拒会让非 git 项目整个索引不了。

栈中立：全部判据是路径/文件名/文本层面，不含任何语言或构建系统假设。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from swarm.infra.degrade import record_degrade

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 层1·安全：敏感文件名（fail-closed 兜底，不依赖 .gitignore）
# ──────────────────────────────────────────────
# 纪律：这张表是"就算项目没写 .gitignore 也绝不入库"的最后防线，因此按【文件名形态】
# 匹配而非扩展名——`.env` 无扩展名、`id_rsa` 无扩展名，正是它们绕过 EXCLUDED_EXTENSIONS
# 的原因。命名变体（.env.local/.env.bak-round38/dev.env）一并覆盖。
# ★W1 复核 HIGH-3 整改：正则必须排除【源码扩展名】★
# 初版 `^\.?env(\.|$)` / `^credentials(\.|$)` / `(^|[.\-_])secrets?(\.[a-z0-9]+)?$` /
# `\.(pem|key|...)$` 把一等源码一并杀掉，复核实测误杀：
#   env.py（alembic 每个项目都有 migrations/env.py）、env.ts/js/go、Credentials.java
#   （Java 极常见类名）、credentials.py（google-auth/boto 标准模块名）、secrets.py
#   （Python 标准库同名）、Secret.java、jwt_secret.py、messages.key（i18n）、test_env
# 而误杀的后果比漏挡更隐蔽：拒绝后既不索引也不留痕，且会主动删除已有向量。
# 判据改为"名字像凭据 **且** 不是源码/资源文件"。
_CODE_EXTS = (
    "py", "ts", "tsx", "js", "jsx", "mjs", "cjs", "java", "kt", "kts", "scala", "go",
    "rs", "rb", "php", "cs", "c", "cc", "cpp", "h", "hpp", "swift", "m", "mm",
    "sh", "bash", "zsh", "sql", "html", "htm", "css", "scss", "less", "vue", "svelte",
    "md", "rst", "txt", "gradle", "tf",           # tf=terraform 代码（tfvars/tfstate 才敏感）
)
_CODE_EXT_RE = re.compile(r"\.(" + "|".join(_CODE_EXTS) + r")$", re.I)

_SENSITIVE_NAME_RES: tuple[re.Pattern[str], ...] = (
    # dotenv 约定：`.env` / `.env.local` / `.env.bak-x` / `prod.env`（但 env.py/env.ts 不是）
    re.compile(r"^\.env(\.|$)", re.I),
    re.compile(r"(^|[.\-])env$", re.I),           # prod.env / dev-env（不含 test_env 那种下划线源码名）
    re.compile(r"^\.?(npmrc|netrc|pypirc|dockercfg)$", re.I),
    re.compile(r"^\.?git-credentials$", re.I),
    re.compile(r"^(id_rsa|id_dsa|id_ecdsa|id_ed25519)(\.|$)", re.I),
    # 私钥/密钥库容器：`.key` 刻意【不在】此列——`messages.key`/`zh_CN.key`(i18n)、
    # `Presentation.key`(Keynote) 全是正常文件；真私钥带 PEM 头，交内容闸的
    # "Private Key" CRITICAL 模式抓，那条判据比扩展名可靠得多。
    re.compile(r"\.(pem|p12|pfx|jks|keystore|kdbx|ppk|p8|pkcs12|ovpn|p7b|gpg)$", re.I),
    re.compile(r"^credentials(\.|$)", re.I),
    re.compile(r"(^|[.\-_])secrets?$", re.I),     # `.secrets` / `app.secrets`（secrets.py 已被源码扩展名排除）
    re.compile(r"^\.?(htpasswd|pgpass|my\.cnf)$", re.I),
    re.compile(r"^service[-_]?account.*\.json$", re.I),
    # 复核 MEDIUM 补漏：业界头号泄露源
    re.compile(r"^kubeconfig(\.|$)", re.I),
    re.compile(r"\.tfvars(\.json)?$", re.I),
    re.compile(r"\.tfstate(\.backup)?$", re.I),
)

# `.env.example` / `.env.template` 这类是**给人看的样板**，值是占位符，索引它们对
# "项目怎么配置"是有效知识。但仍要过内容密钥扫描（样板里有人填真值是常见事故）。
_SENSITIVE_NAME_ALLOW_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\.(example|sample|template|dist|tpl)$", re.I),
    re.compile(r"^\.env\.(example|sample|template)$", re.I),
)


def is_sensitive_filename(name: str) -> bool:
    """按文件名判定是否为凭据/密钥类文件（层1，与 .gitignore 无关）。

    三步：① 样板（.example/.template）放行；② **源码/资源扩展名放行**（复核 HIGH-3：
    `env.py`/`Credentials.java`/`secrets.py` 是一等源码，不是凭据）；③ 再配敏感模式。
    """
    base = os.path.basename(name).strip()          # 复核 LOW：尾随空格绕过
    if any(r.search(base) for r in _SENSITIVE_NAME_ALLOW_RES):
        return False
    if _CODE_EXT_RE.search(base):
        return False
    return any(r.search(base) for r in _SENSITIVE_NAME_RES)


class SecretScanUnavailable(RuntimeError):
    """密钥扫描器不可用——调用方必须按【可重试的暂时故障】处理，绝不当作"无命中"。

    W1 复核 C2：初版让本情形返回一个拒绝原因串，而调用方对拒绝是 `return 0` 不抛，
    于是不进 `kb_pending_embeddings` 重试队列；而 Layer A 已把 last_modified 刷成 now()
    → 一致性巡检既不判 missing（行在）也不判 stale（时间新）→ **该文件的向量永久丢失
    且无任何自愈路径**。改为独立异常类型，让它与 EmbeddingUnavailableError 同构地
    走既有重试语义。
    """


def content_secret_hits(text: str) -> list[tuple[str, str]]:
    """入库前内容密钥扫描——复用交付闸的单一事实源，不另造第二张模式表。

    `worker/security_scan.scan_text_for_secrets` 是 MERGE 交付 diff / AUDIT 项目扫描 /
    经验技能导入准入共用的那张表（纯 re、stack-neutral、无 IO），返回
    `[(pattern_name, 脱敏片段)]` 且实现内保证绝不回吐全文。

    ★只取 CRITICAL 档（W1 复核 HIGH-2）★：该表的 HIGH 档是**刻意的 FP 控制设计**——
    `Generic Secret Assignment` 会命中 RuoYi 基线的 `CSRF_TOKEN = "csrf_token"`（常量名
    非密钥），DR-05-F5(#85) 对抗复核为此裁定 HIGH=warn 不阻断，其消费契约是
    "MERGE 命中走 escalate **人工复核（非硬丢）**，误报由人一眼放行"。
    知识库入库闸没有人工复核环节、拒绝即丢弃且会删存量，**后果严重性完全不同**，
    照搬全档＝把当年裁定的冤杀重新引入（复核实测本仓 1.7% 源文件受害，第一个就是
    冤杀实证本尊 ShiroConstants.java）。故此处按自身后果选 CRITICAL 阈值——
    共享模式表不变（单一事实源），**消费契约随后果分档**。
    """
    from swarm.worker.security_scan import scan_text_for_secrets
    return scan_text_for_secrets(text or "", min_severity="critical")


# ──────────────────────────────────────────────
# 层2·降噪：.gitignore 遵守
# ──────────────────────────────────────────────
class GitignoreFilter:
    """用 `git check-ignore` 判定忽略——不自研 gitignore 语义。

    为什么走 git 而不是自研/引依赖：`.gitignore` 的真实语义（`!` 取反、目录锚定、
    `**` 跨层、层级叠加、`.git/info/exclude`、global excludesfile）自研极易出错，
    而错的方向是**漏挡**（噪声照进）或**误挡**（真源码被丢）。直接问 git 语义 100% 正确、
    零新依赖。批量走 `--stdin` 一次调用，性能与文件数无关。

    嵌套仓库：项目根**不一定**是 git 仓库根（实测 test-1 的 root 是 `~/LLM/swarm`，
    真仓库在其子目录 `swarm/`）。故按"文件所属最近仓库"分组批量判定。

    fail-open：git 不可用、非仓库、调用失败一律【放行】——本层只做降噪，
    安全由层1 独立兜底，绝不让 git 缺席变成安全缺口。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._repo_cache: dict[Path, Path | None] = {}
        self._git_ok = self._probe_git()

    @staticmethod
    def _probe_git() -> bool:
        try:
            subprocess.run(["git", "--version"], capture_output=True, timeout=5, check=True)
            return True
        except Exception:  # noqa: BLE001 — git 缺席=本层整体降级放行
            logger.info("[INGEST-GUARD] git 不可用 → .gitignore 降噪层跳过（安全层不受影响）")
            return False

    def _repo_root_for(self, abs_dir: Path) -> Path | None:
        """找 abs_dir 所属的 git 仓库根（向上找 .git，**严格止于 self.root**）。

        W1 复核 HIGH-5/MEDIUM 两处整改：
        ① 边界：初版终止条件是 `cur == self.root.parent` 且 `.git` 探测在终止判断【之前】，
           于是会检查并采用**项目根的父目录**那一层的仓库。若父目录是个仓库且其 .gitignore
           忽略了本项目（vendored 子目录 / monorepo 里被忽略的 legacy 模块 / 上传到某仓库
           ignored 目录下的用户项目），整个项目会被判全量 ignored → 知识库全空。
           改为严格止于 self.root：**项目外的 .gitignore 规则不该管项目内的事**。
        ② 跳数上限：初版 `hops <= max_repo_depth + 8` 是与"嵌套深度"无关的固定 11 跳预算，
           超预算即返回 None 静默放行。复核在真实 RuoYi 基线实测有 111 个深度 15 的
           static/vendored 文件因此逃逸——**恰是本闸想清掉的噪声**。self.root 本身就是
           天然边界，无需人为上限（实测 23663 文件全量 0.51s，且按目录缓存）。
        """
        if abs_dir in self._repo_cache:
            return self._repo_cache[abs_dir]
        cur = abs_dir
        found: Path | None = None
        while True:
            if (cur / ".git").exists():
                found = cur
                break
            if cur == self.root or cur == cur.parent:
                break                      # 严格止于项目根（含 root 自身已在上面判过）
            cur = cur.parent
        self._repo_cache[abs_dir] = found
        return found

    def filter_ignored(self, rel_paths: list[str]) -> set[str]:
        """返回 rel_paths 中被 .gitignore 忽略的子集（相对 self.root）。"""
        if not self._git_ok or not rel_paths:
            return set()
        by_repo: dict[Path, list[str]] = {}
        for rel in rel_paths:
            abs_p = (self.root / rel)
            repo = self._repo_root_for(abs_p.parent)
            if repo is not None:
                by_repo.setdefault(repo, []).append(rel)
        ignored: set[str] = set()
        for repo, rels in by_repo.items():
            try:
                # check-ignore 的输入需相对该仓库；用绝对路径最省事（git 接受）
                payload = "\n".join(str(self.root / r) for r in rels)
                proc = subprocess.run(
                    ["git", "-C", str(repo), "check-ignore", "--stdin"],
                    input=payload, capture_output=True, text=True, timeout=60,
                )
                # rc=0 有命中 / rc=1 无命中 / rc>1 出错（出错按放行处理）
                if proc.returncode > 1:
                    logger.info("[INGEST-GUARD] check-ignore 于 %s 返回 rc=%d → 该仓库降噪跳过",
                                repo, proc.returncode)
                    continue
                hit_abs = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
                for r in rels:
                    if str(self.root / r) in hit_abs:
                        ignored.add(r)
            except Exception as exc:  # noqa: BLE001 — 降噪层 fail-open
                record_degrade("knowledge.ingest_guard.gitignore_unavailable")
                logger.warning("[INGEST-GUARD] check-ignore 于 %s 失败（降噪跳过，安全层不受影响）: %s",
                               repo, exc)
        # ★自保 backstop（W1 复核 HIGH-5）★：降噪层是 fail-open 层，"拿不准就不降噪"。
        # 若它要清掉几乎整个项目，几乎必然是判据出了问题（项目位于某个忽略它的仓库内、
        # .gitignore 写了 `*`、仓库归属判错），而下游 `_save_file_index` 对空 list 是
        # 静默早返回 → 项目照常 READY、知识库全空、无人知晓。宁可这一轮不降噪。
        if rel_paths and len(ignored) >= max(20, int(len(rel_paths) * 0.9)):
            record_degrade("knowledge.ingest_guard.gitignore_backstop")
            logger.warning(
                "[INGEST-GUARD] .gitignore 判定要清掉 %d/%d（≥90%%）个文件——判据可疑"
                "（项目是否位于某个忽略它的仓库内？），本轮【整体放弃降噪】以免知识库被清空",
                len(ignored), len(rel_paths))
            return set()
        return ignored


# ──────────────────────────────────────────────
# 统一判据入口
# ──────────────────────────────────────────────
# 众所周知的【凭据目录】——`.docker/config.json`、`.kube/config` 这类文件名本身不敏感，
# 敏感的是它所在的目录。刻意用【短黑名单】而非"隐藏目录全拒"：见 credential_reject_reason。
_CREDENTIAL_DIRS = frozenset({
    ".ssh", ".aws", ".gnupg", ".gpg", ".docker", ".kube", ".azure", ".gcloud",
    ".config/gcloud", ".chef", ".subversion",
})


def credential_reject_reason(rel_path: str) -> str | None:
    """**只判凭据**的路径闸——给"必须保持项目可构建"的消费方用（如源码 tarball）。

    与 `reject_reason_by_name` 的差别只有一条：**不拒隐藏目录**。
    这条差别是被实证逼出来的（R2 复核 MEDIUM）：把入库闸整个搬去做镜像 tarball 剔除后，
    `.mvn/wrapper/maven-wrapper.properties` 与 `.yarn/releases/yarn-*.cjs` 双双被判
    `hidden_dir` 剔除 → 用 `./mvnw` / yarn Berry 的项目在沙箱里必然构建失败，且失败信息
    指向"找不到 wrapper jar"，与"tarball 少打了文件"毫无关联，排查成本极高。同时违反
    多栈中立（只打击 Maven wrapper / Yarn Berry 两个生态）。

    根因是**消费契约不同**，不是判据写错：对知识库，隐藏目录是噪声；对构建 tarball，
    隐藏目录可能是工具链本体。故按契约分函数，而不是给共用函数加开关参数（同一个名字
    在两种语义间漂移，正是当初把入库闸整块复用过来的那个错误）。

    凭据目录仍然拦（`.ssh/`、`.aws/`、`.docker/config.json`）——用短黑名单而非
    "构建目录白名单"：凭据目录是一张封闭的、业界公认的短表；而构建工具链的隐藏目录
    随生态无限增长，白名单必然变成"补一个漏一个"。
    """
    base = os.path.basename(rel_path)
    if is_sensitive_filename(base):
        return "sensitive_filename"
    parts = Path(rel_path).parts
    lowered = [seg.lower() for seg in parts[:-1]]
    for i, seg in enumerate(lowered):
        if seg in _CREDENTIAL_DIRS or "/".join(lowered[i:i + 2]) in _CREDENTIAL_DIRS:
            return "credential_dir"
    if base.startswith("."):
        if base.lower() in _HIDDEN_FILE_ALLOW:
            return None
        if any(r.search(base) for r in _SENSITIVE_NAME_ALLOW_RES):
            return None
        return "hidden_file"
    return None


def reject_reason_by_name(rel_path: str) -> str | None:
    """仅按【路径/文件名】判定是否拒绝入库；返回拒绝原因，None=该层放行。

    这是三条通道都必须先过的最小闸（不读文件内容，零 IO）。
    """
    base = os.path.basename(rel_path)
    if is_sensitive_filename(base):
        return "sensitive_filename"
    # 隐藏【目录】段——全量通道靠 os.walk 原地剪 dirnames 挡住，但增量/一致性通道
    # 拿到的是现成路径不走 walk，于是 `.claude/*`、`.hermes/plans/*` 被重新塞回索引
    # （审计实测 16 个隐藏文件 + 11 个隐藏目录下文件）。判据下沉到本模块后三通道同源。
    parts = Path(rel_path).parts
    # ★30 号文批22 F-6★：CI 定义目录显式放行——「隐藏目录=噪声」对 CI 知识是误杀
    # （.github/workflows 的 pipeline 定义是「项目怎么构建/发版」的一等知识）。
    # 短白名单（封闭集，业界公认），绝不长成「补一个漏一个」的通用豁免表；
    # 内容密钥闸在下游照常兜底（本闸只过名字层）。不放行整个 .github/——
    # ISSUE_TEMPLATE/dependabot 等仍按 hidden_dir 拒，范围刻意只到 workflows。
    norm = "/".join(parts)
    _ci_prefix = next((d for d in _CI_DIR_ALLOW if norm.startswith(d + "/")), None)
    # ★R1 折 hunter F3★：白名单只豁免【前缀段本身】——前缀之下再嵌隐藏目录
    # （.github/workflows/.cache/token.txt）不是 CI 知识，剩余段仍跑点段检查，
    # 否则隐藏目录段会被整个放行（误放行越界）。
    _segs = parts[:-1]
    if _ci_prefix is not None:
        _segs = _segs[len(_ci_prefix.split("/")):]
    for seg in _segs:
        if seg.startswith(".") and seg not in (".", ".."):
            return "hidden_dir"
    # 隐藏【文件】——全量通道只挡隐藏目录不挡隐藏文件，`.env` 正是从这个洞进来的。
    # 两类例外显式放行：① 广为人知的工程配置文件；② `*.example/.sample/.template` 样板
    # （值是占位符、是"项目怎么配置"的有效知识；万一有人填了真值，由内容密钥闸兜底）。
    if base.startswith("."):
        if base.lower() in _HIDDEN_FILE_ALLOW:
            return None
        if any(r.search(base) for r in _SENSITIVE_NAME_ALLOW_RES):
            return None
        return "hidden_file"
    return None


_HIDDEN_FILE_ALLOW = {
    ".gitignore", ".gitattributes", ".dockerignore", ".editorconfig",
    ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".prettierrc",
    ".prettierrc.json", ".babelrc", ".nvmrc", ".python-version",
    ".ruby-version", ".tool-versions", ".pre-commit-config.yaml",
    # 批22 F-6：根级 CI 定义文件（与 _CI_DIR_ALLOW 同契约——CI 知识不是噪声）
    ".gitlab-ci.yml", ".drone.yml",
}

# 批22 F-6：CI 定义【目录】白名单（reject_reason_by_name 的 hidden_dir 层放行）。
# 封闭短表：GitHub Actions / CircleCI。GitLab/Drone 是根级文件（在 _HIDDEN_FILE_ALLOW）。
# ★根锚定是刻意的★（R1 折 reviewer LOW-2）：monorepo 子包的 packages/a/.github/...
# 照拒 hidden_dir——白名单只承诺「仓库根的 CI 定义」。
_CI_DIR_ALLOW = frozenset({".github/workflows", ".circleci"})

# 全量通道 walk 剪枝消费的根层例外（project/preprocess._scan_sync）：
# 白名单条目的第一段。★派生自 _CI_DIR_ALLOW 单一事实源，绝不另抄第二份表★——
# 不加这一层，.github 在 walk 期就被剪掉，名字闸放行在主发现通道是死代码
# （R1 折 reviewer M-1 + hunter F2 双透镜同洞：「闸存在≠覆盖面完整」）。
CI_DIR_ALLOW_ROOTS = frozenset({d.split("/", 1)[0] for d in _CI_DIR_ALLOW})


class IngestGate:
    """三条入库通道共用的准入闸对象（W1 复核 HIGH-1 整改）。

    初版只把降噪层接进了全量通道，而增量/一致性通道仍只过名字层——于是"准入面倒挂"
    与"每日 04:00 不收敛"这两个**主症状**原样存续：preprocess 按 .gitignore 排除 →
    kb_file_index 无该行 → consistency 判 missing → 入队 → updater 名字层放行 → 重新嵌入。
    病灶只是从 A 通道搬到了 B 通道。

    本对象把两层判据打包，三通道各构造一次即可同源：
      - 全量 preprocess：批量 `filter_ignored`（一次 git 调用）
      - 增量 updater：单文件 `is_ignored`
      - 一致性 consistency：批量或单文件均可
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._gitignore = GitignoreFilter(self.root)

    def reject_by_name(self, rel_path: str) -> str | None:
        """层1 安全 + 隐藏路径（零 IO）。"""
        return reject_reason_by_name(rel_path)

    def is_ignored(self, rel_path: str) -> bool:
        """层2 降噪·单文件。批量场景请用 filter_ignored（一次 git 调用更省）。"""
        return bool(self._gitignore.filter_ignored([rel_path]))

    def filter_ignored(self, rel_paths: list[str]) -> set[str]:
        """层2 降噪·批量。"""
        return self._gitignore.filter_ignored(rel_paths)

    def reject_for_indexing(self, rel_path: str) -> str | None:
        """通道统一入口：名字层 + 降噪层合判，返回拒绝原因（None=放行）。

        注意内容层不在此——它需要文件内容，且只在【写向量】那一层做（Layer A 只存
        path/language/hash，不含内容，不必受内容闸约束；见 reject_reason_by_name 的
        分层说明）。
        """
        _rej = self.reject_by_name(rel_path)
        if _rej:
            return _rej
        if self.is_ignored(rel_path):
            return "gitignored"
        return None


def reject_reason_by_content(rel_path: str, text: str) -> str | None:
    """按【内容】判定是否拒绝入库（层1 安全闸的第二道）。只认 CRITICAL 档，见上。

    扫描器不可用 → 抛 `SecretScanUnavailable`（不是返回拒绝原因）：那是**暂时故障**
    而非"这是敏感文件"，必须走调用方的重试语义，否则该文件的向量会永久丢失且无自愈
    路径（W1 复核 C2）。fail-closed 体现在"不当作无命中放行"，而不是"当作敏感丢弃"。
    """
    try:
        hits = content_secret_hits(text)
    except Exception as exc:  # noqa: BLE001
        record_degrade("knowledge.ingest_guard.scanner_unavailable")
        logger.warning("[INGEST-GUARD] 密钥扫描器不可用 → 按暂时故障抛出待重试 %s: %s",
                       rel_path, exc)
        raise SecretScanUnavailable(str(exc)) from exc
    if hits:
        # 只记 pattern 名与脱敏片段（scan_text_for_secrets 内部已保证不回吐全文）
        record_degrade("knowledge.ingest_guard.secret_content_rejected")
        logger.warning("[INGEST-GUARD] 内容命中 CRITICAL 密钥模式 → 拒绝入库 %s: %s",
                       rel_path, [h[0] for h in hits])
        return f"secret_content:{hits[0][0]}"
    return None
