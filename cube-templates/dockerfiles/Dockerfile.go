# Go 依赖预热模板 — Go 1.22 + 预热 GOMODCACHE
FROM ghcr.io/tencentcloud/cubesandbox-base:2026.16

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# 装 Go
ENV GO_VERSION=1.22.5
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -o /tmp/go.tgz \
    && tar -C /usr/local -xzf /tmp/go.tgz && rm /tmp/go.tgz
ENV PATH="/usr/local/go/bin:${PATH}"
ENV GOPATH=/root/go
ENV GOMODCACHE=/root/go/pkg/mod
# 国内代理加速（构建期）
ENV GOPROXY=https://goproxy.cn,direct

# 预热：用 warmup go.mod 下载常用依赖进 GOMODCACHE
COPY warmup/go.mod /opt/warmup/go.mod
RUN cd /opt/warmup && (go mod download all || true)

RUN go version || true
