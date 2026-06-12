// 预热常用 Go 依赖进 GOMODCACHE（沙箱克隆后 go build 免下载）。按需增删。
module dep-warmup

go 1.22

require (
	github.com/gin-gonic/gin v1.10.0
	github.com/go-sql-driver/mysql v1.8.1
	gorm.io/gorm v1.25.10
	gorm.io/driver/mysql v1.5.6
	github.com/spf13/cobra v1.8.0
	github.com/spf13/viper v1.18.2
	go.uber.org/zap v1.27.0
	github.com/stretchr/testify v1.9.0
	github.com/redis/go-redis/v9 v9.5.1
)
