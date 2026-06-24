# Go 依赖预热模板 — Go 1.22 + 预热 GOMODCACHE
FROM ghcr.io/tencentcloud/cubesandbox-base:2026.16

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# 装 Go
# go.dev 在沙箱构建机网络被墙（实测 SSL_ERROR_SYSCALL）→ 用阿里云 Go 镜像（实测 200）
ENV GO_VERSION=1.22.5
RUN curl -fsSL "https://mirrors.aliyun.com/golang/go${GO_VERSION}.linux-amd64.tar.gz" -o /tmp/go.tgz \
    && tar -C /usr/local -xzf /tmp/go.tgz && rm /tmp/go.tgz
ENV PATH="/usr/local/go/bin:/root/go/bin:${PATH}"
ENV GOPATH=/root/go
ENV GOMODCACHE=/root/go/pkg/mod
# 国内代理加速（构建期）
ENV GOPROXY=https://goproxy.cn,direct

# 预热：用 warmup go.mod 下载常用依赖进 GOMODCACHE
COPY warmup/go.mod /opt/warmup/go.mod
RUN cd /opt/warmup && (go mod download all || true)

# 确定性修复工具：goimports（Go 事实标准 import autofix，L1 _repair_go 用）。
# GOBIN=/usr/local/bin 落在非登录 shell 的 PATH 上（sandbox.commands.run 不读 profile）。
RUN GOBIN=/usr/local/bin go install golang.org/x/tools/cmd/goimports@v0.24.0 || true

RUN go version || true
RUN command -v goimports >/dev/null 2>&1 && echo "goimports OK" || echo "goimports MISSING"
