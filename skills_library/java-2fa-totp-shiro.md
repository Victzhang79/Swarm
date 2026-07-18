---
id: java-2fa-totp-shiro
title: 双因子认证 2FA/TOTP 与 Shiro 自定义过滤器（Java）
description: "当你在 Java 项目里实现双因子认证/两步验证/2FA/TOTP/一次性验证码/Google Authenticator，用 dev.samstevens.totp 库生成/校验验证码，或写 Shiro 自定义登录过滤器（AccessControlFilter）时调用。返回正确的 totp 1.7.1 API、Shiro 过滤器骨架、以及'引入第三方库前先补坐标'与 pom 最小增量铁律。"
applies_to_stacks: ["java"]
applies_to_intents: ["create", "modify", "debug"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 60
max_chars: 3600
tags: ["java", "2fa", "totp", "otp", "shiro", "auth", "security", "filter", "dependency"]
---

## 铁律 0：引入第三方库前，先确认坐标已在 pom；缺就先补坐标，再 import

`import dev.samstevens.totp.*`（或任何第三方包）能编译的前提是**该库的 Maven 坐标已在可达 pom 里声明**。基线项目**默认没有** totp 库——只写 import 不加坐标 = `package ... does not exist`，换几个模型都编不过（jar 不在 classpath，不是代码问题）。

所以：**先在本模块 `pom.xml` 的 `<dependencies>` 里追加坐标（带显式 version），再写 import 和用法**。totp 的坐标：

```xml
<dependency>
    <groupId>dev.samstevens.totp</groupId>
    <artifactId>totp</artifactId>
    <version>1.7.1</version>
</dependency>
```

## 正确的 dev.samstevens.totp 1.7.1 API（照抄，别臆造）

```java
import dev.samstevens.totp.code.*;                 // CodeGenerator/CodeVerifier/DefaultCodeGenerator/DefaultCodeVerifier/HashingAlgorithm
import dev.samstevens.totp.secret.DefaultSecretGenerator;
import dev.samstevens.totp.secret.SecretGenerator;
import dev.samstevens.totp.time.SystemTimeProvider;
import dev.samstevens.totp.time.TimeProvider;

// 生成共享密钥（Base32 字符串）——方法叫 generate()，不是 generateSecret()
SecretGenerator secretGenerator = new DefaultSecretGenerator();      // 可选 new DefaultSecretGenerator(64)
String secret = secretGenerator.generate();

// 构造校验器——构造器签名是 (CodeGenerator, TimeProvider)，没有 .build()、没有 builder
CodeGenerator codeGenerator = new DefaultCodeGenerator(HashingAlgorithm.SHA1, 6);
TimeProvider  timeProvider  = new SystemTimeProvider();
DefaultCodeVerifier verifier = new DefaultCodeVerifier(codeGenerator, timeProvider);
verifier.setAllowedTimePeriodDiscrepancy(1);   // 可选：允许 ±1 个时间窗

// 校验用户输入的 6 位码
boolean valid = verifier.isValidCode(secret, userCode);
```

**这些是幻觉，绝不要写**：`DefaultCodeVerifier.build()`、`SecretGenerator.generateSecret()`、`new DefaultCodeVerifier(HashingAlgorithm,int,int)`、`dev.samstevens.totp.generator` 包、`dev.samstevens.totp.util.Base64`、`dev.samstevens.totp.secret.Secret`、`TotpHumanReadableGenerator`。二维码/otpauth 用 `dev.samstevens.totp.qr`（QrData.Builder / ZxingPngQrGenerator）+ `dev.samstevens.totp.util.Utils.getDataUriForImage(...)`。Base64 用 `java.util.Base64`。

## Shiro 自定义过滤器骨架（Shiro 2.x / Jakarta）

继承 `org.apache.shiro.web.filter.AccessControlFilter`（是 `web.filter`，**不是** `web.filter.authc`）。用 `jakarta.servlet.*`（不是 `javax`）。**先读同仓最近的样板**（如 `CaptchaValidateFilter`）照抄其 superclass 与 import，别凭记忆。

```java
import org.apache.shiro.web.filter.AccessControlFilter;
import jakarta.servlet.ServletRequest;
import jakarta.servlet.ServletResponse;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

public class TwoFactorAuthFilter extends AccessControlFilter {
    @Override
    protected boolean isAccessAllowed(ServletRequest req, ServletResponse resp, Object mappedValue) {
        // 返回 true 放行；false 走 onAccessDenied
        return /* 已通过 2FA? */ true;
    }
    @Override
    protected boolean onAccessDenied(ServletRequest req, ServletResponse resp) throws Exception {
        // 拦截处理（跳转/返回 401/写 2FA 挑战）；返回 false 表示已处理、不再进链
        return false;
    }
}
```
注册：在 ShiroConfig 的 `filterChainDefinitionMap` / filters map 里挂上，仿既有过滤器的注册方式。

## pom 最小增量铁律（st-53-1 血泪）

改 pom **只允许**在既有 `<dependencies>` 里**追加**一个 `<dependency>`，其余节点**逐字节不动**。

- **绝不重写结构标签**：`<groupId>` 写成 `<group>`、动 `<parent>`/`<artifactId>`/`<modelVersion>` = `Unrecognised tag` / `'parent.groupId' is missing`，POM 解析期整树炸。
- 子模块**没有自己的** `<groupId>`/`<version>`——它们从 `<parent>` 继承；只保留 `<artifactId>`。
- 版本写不写的判据见 [maven-dependency-management]：父/BOM 管得到就不写，第三方管不到就写显式 version。

## 文件完整、不截断

Java 文件必须语法完整：大括号配平、无"reached end of file"、无 `HttpServletServletResponse` 这类名字粘连。filter+service+pom 三件一起改时，**逐个文件写完整并自查每个都闭合**，宁可分文件写全，不要半截。
