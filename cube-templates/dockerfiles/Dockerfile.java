# Java 依赖预热模板 — OpenJDK 21 + Maven + 预热 .m2
# 4c4g 用途。构建期下载常用依赖固化进镜像，沙箱启动即有 ~/.m2 缓存。
FROM ghcr.io/tencentcloud/cubesandbox-base:2026.16

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openjdk-21-jdk maven git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# 用 Aliyun 镜像加速依赖下载（构建期），并固化进镜像 settings.xml
RUN mkdir -p /root/.m2
COPY warmup/settings.xml /root/.m2/settings.xml

# 预热：用一个聚合常用依赖的 warmup pom 执行 go-offline，
# 把 Spring Boot / MyBatis / 常用 starter 等下载进 ~/.m2。
COPY warmup/pom.xml /tmp/warmup/pom.xml
RUN cd /tmp/warmup \
    && (mvn -q -B dependency:go-offline || true) \
    && (mvn -q -B compile || true) \
    && rm -rf /tmp/warmup/target

# 校验工具链可用（不阻断构建）
RUN java -version && mvn -v || true

# envd 由 cube-entrypoint.sh 在后台拉起（base 镜像已配）；无前台 CMD 时 envd 为前台。
