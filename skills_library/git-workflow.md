---
id: git-workflow
title: Git 工作流与提交规范
description: "当你在选分支策略（GitHub Flow/GitFlow）、写 Conventional Commits 提交、纠结 merge 还是 rebase、解冲突标记或打 SemVer tag 发版时调用，返回 Git 全流程规范速查。"
enabled: false  # 阶段E 下架：内容(push/tag/PR)与无 .git 沙箱矛盾且 phases=produce 永不选中（G5）
applies_to_stacks: ["*"]
applies_to_intents: ["*"]
applies_to_phases: ["produce"]
target: ["worker"]
priority: 40
max_chars: 1800
tags: ["git", "workflow", "commit"]
---

**分支策略**
- GitHub Flow（多数场景推荐）：`main` 恒可部署，从 main 切 feature，PR + CI 过后合回，合后即部署
- Trunk-Based（高速团队）：直提 main 或 1-2 天短命分支，未完工用 feature flag 藏，多次/天部署
- GitFlow（排期发版/企业）：`main` 仅生产，`develop` 集成，release/hotfix 双向合入 main+develop

**Conventional Commits**：`<type>(<scope>): <subject>` + 可选正文（解释 why 非 what）+ footer。type：feat/fix/docs/style/refactor/test/chore/perf/ci/revert。subject 祈使句、无句号、≤50 字符。
- 差：`fixed stuff`/`update`/`WIP`
- 好：`fix(api): retry on 503`，正文说明原因，footer `Closes #123`

**merge vs rebase**
- merge：保留历史，用于合 feature 入 main、多人协作分支、已推送分支
- rebase：线性历史，用于本地未推送分支同步 main、单人分支
- 绝不 rebase：已推送/他人基于其工作/受保护/已合并的分支（改写历史会毁他人工作）
- 同步：`git fetch origin && git rebase origin/main`，仅单人时 `git push --force-with-lease`

**PR**：标题同 commit 格式；描述含 What/Why/How/Testing；PR ≤500 行、单一主题；作者先自审、CI 绿（test+lint+typecheck）；审查看：解决问题？边界？可读？测试足？安全？

**冲突**：`git status` 看冲突文件；改标记 `<<<<<<< / ======= / >>>>>>>` 保正确内容；或 `git checkout --ours/--theirs <f>`；`git add` 后 commit。预防：分支小而短命、勤 rebase、用 feature flag。

**分支命名**：`feature/*` `fix/*` `hotfix/*` `release/x.y.z`。清理：`git fetch -p`；`git branch -d`（已合）/`-D`（强删）。

**发版**：SemVer `MAJOR.MINOR.PATCH`（破坏/新功能/修复）。`git tag -a vX.Y.Z -m "..."` + `git push origin vX.Y.Z`。

**撤销**：`reset --soft HEAD~1`（留改动）/`--hard`（弃）；已推送用 `git revert HEAD`（禁 force push 公共分支）；`commit --amend` 改上条。

**反模式**：直提 main；提交 `.env`/密钥（入 .gitignore）；巨型 PR；提交 `dist/`、`node_modules/` 等生成物；force push 公共历史。
