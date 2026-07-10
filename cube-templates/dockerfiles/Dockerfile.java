# Java 依赖预热模板 — OpenJDK 17 + Maven + 预热 .m2（对齐 ruoyi-e2e: JDK17 / Spring Boot 4.0.6）
# 4c4g 验证镜像。构建期下载 ruoyi-e2e 真实依赖固化进 .m2，沙箱克隆即有缓存 → 首个 mvn 走本地。
# 注意：JDK 版本与项目 <java.version>17 对齐（之前误用 21，向后兼容但不严谨）。
FROM ghcr.io/tencentcloud/cubesandbox-base:2026.16

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openjdk-17-jdk maven ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# 镜像源加速（构建期），固化进镜像 settings.xml
RUN mkdir -p /root/.m2
COPY warmup/settings.xml /root/.m2/settings.xml

# 预热：warmup pom 列 ruoyi-e2e 真实【外部第三方依赖】(SB4.0.6/Shiro2.2/Druid-SB4/MyBatis4...)，
# 排除项目内部模块(com.ruoyi:ruoyi-*，运行时现编)。go-offline 把外部依赖拉满进 .m2。
COPY warmup/pom.xml /tmp/warmup/pom.xml
RUN cd /tmp/warmup \
    && (mvn -q -B -Dmaven.test.skip=true dependency:go-offline || true) \
    && (mvn -q -B -Dmaven.test.skip=true compile || true) \
    && rm -rf /tmp/warmup/target

# 自测①：离线编译 warmup pom —— 离线能过 = .m2 真填满了（卡点⑤：验证 warmup 生效）
RUN cd /tmp/warmup \
    && echo "=== warmup 离线自测(mvn -o)===" \
    && (mvn -o -q -B -Dmaven.test.skip=true compile \
        && echo "✅ warmup 离线编译通过：.m2 缓存已填满" \
        || echo "⚠️ warmup 离线编译失败：部分依赖未命中缓存，检查 settings.xml 镜像源是否覆盖 SB4.x/Shiro/yauaa 等") \
    && rm -rf /tmp/warmup/target

# F2（round38c 主题F，register #18/#35 后半）：补 lint/formatter——L1 lint 层的
# checkstyle 与 format 层的 google-java-format 此前在沙箱恒缺席（round38c 实证
# 52/52 lint/format 全 skipped=L1 实为 3 层，可观测性降级）。装上闸门才有牙。
# jar 走阿里云 Maven 镜像（对齐 settings.xml 国内源；GitHub release 内网不稳）。
# checkstyle wrapper 内置 -c /google_checks.xml（jar classpath 自带配置——L1 的
# _lint_java 裸跑不带 -c，无默认配置时 CLI 必非 0 被 only_error_if_issues 静默跳过）。
RUN apt-get update && apt-get install -y --no-install-recommends wget \
    && mkdir -p /opt/lint \
    && wget -q -O /opt/lint/checkstyle.jar \
        https://maven.aliyun.com/repository/public/com/puppycrawl/tools/checkstyle/10.17.0/checkstyle-10.17.0-all.jar \
    && wget -q -O /opt/lint/google-java-format.jar \
        https://maven.aliyun.com/repository/public/com/google/googlejavaformat/google-java-format/1.22.0/google-java-format-1.22.0-all-deps.jar \
    && printf '#!/bin/sh\nexec java -jar /opt/lint/checkstyle.jar -c /google_checks.xml "$@"\n' \
        > /usr/local/bin/checkstyle \
    && printf '#!/bin/sh\nexec java -jar /opt/lint/google-java-format.jar "$@"\n' \
        > /usr/local/bin/google-java-format \
    && chmod +x /usr/local/bin/checkstyle /usr/local/bin/google-java-format \
    && rm -rf /var/lib/apt/lists/* \
    && (checkstyle --version && google-java-format --version || true)

# 校验工具链（不阻断构建）
RUN java -version && mvn -v || true

# envd 由 cube-entrypoint.sh 在后台拉起（base 镜像已配）；无前台 CMD 时 envd 为前台。
# 构建后请按 cube-templates/README 用 build.sh 做 envd /health 自测再 create-from-image。

# 沙箱工作目录（worker bootstrap/L1/agent bash 均假定 /workspace 存在——v2 镜像缺此目录
# 致 bare 沙箱 cd /workspace 必挂，2026-07-07 运维项实测）
RUN mkdir -p /workspace
WORKDIR /workspace
