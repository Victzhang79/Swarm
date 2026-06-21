# RuoYi 开发规范汇编(自 https://doc.ruoyi.vip/ruoyi/ 全量蒸馏)

> 本文由 swarm 遍历 RuoYi 官方文档 15 页蒸馏而成,作为生成 RuoYi 风格代码的规范依据。
> 同步以离散 norms(关键词检索)+ 语义向量(相似度检索)灌入 ruoyi-e2e 知识库,worker 随用随取。
> 版本:经典单体版(Shiro/Thymeleaf,非前后端分离)。

## 一、工程结构与环境

RuoYi 多模块工程的整体结构、依赖分层、环境与配置约定。

### RuoYi 技术栈与版本分支  `(p6)`

RuoYi 是基于经典组合 SpringBoot + Apache Shiro + MyBatis + Thymeleaf + Bootstrap 的 Java EE 后台管理快速开发平台（本仓库为非前后端分离的单体版）。持久层 MyBatis 3.5.x + PageHelper 分页 + Alibaba Druid 连接池 + Hibernate Validation。多分支并行：master(SpringBoot4/JDK17+)、springboot3(JDK17+)、springboot2(JDK8+)。生成后端代码时严格遵循该技术栈，不要引入与之冲突的框架。

### RuoYi 内置业务模块清单  `(p6)`

RuoYi 内置模块：用户管理、部门管理、岗位管理、菜单管理、角色管理、字典管理(sys_dict_type/sys_dict_data)、参数设置(sys_config)、通知公告、操作日志(sys_oper_log)、登录日志、在线用户、定时任务(quartz)、代码生成、系统接口(Swagger)、服务监控、缓存监控、在线表单构建。新增业务模块应复用这些既有模块的命名/分层/注解风格，不要另起一套体系。

### RuoYi 安全特性约定(XSS/CSRF/防注入)  `(p7)`

RuoYi 框架内置安全能力：XSS 跨站脚本过滤(common.xss 包 + @Xss 注解 + xss.excludes 白名单配置)、CSRF 防护、SQL 注入防护(MyBatis 用 #{} 预编译，禁止 ${} 拼接用户输入，仅 ${params.dataScope} 等框架内部允许)、按钮/数据权限控制。生成代码必须沿用这些机制，用户输入一律走校验与转义。

### RuoYi 运行环境版本要求  `(p5)`

运行环境要求：JDK >= 1.8、MySQL >= 5.7、Maven >= 3.0、Redis(用于缓存/会话)、Node.js(前端构建，分离版需要)。MySQL 驱动 v6+ 用 com.mysql.cj.jdbc.Driver，v5 用 com.mysql.jdbc.Driver。Linux 下 MySQL 需设 lower_case_table_names=1 解决表名大小写敏感。

### RuoYi 配置文件分层(application.yml / application-druid.yml)  `(p5)`

配置分层：application.yml 放服务端口(默认80)、context-path、profile(文件上传根路径，需读写权限)、log.path(日志目录)、各功能开关；application-druid.yml 放数据源 url/username/password/driverClassName、多数据源(master/slave)配置。数据库名默认 ry，需导入 ry_*.sql 与 quartz.sql。修改包名/数据源等基础配置改这两个文件。

### RuoYi 文件上传与日志路径配置(profile)  `(p5)`

文件上传路径由 application.yml 的 ruoyi.profile 指定(本地磁盘绝对路径，需读写权限)，上传文件通过 /profile/** 静态资源映射访问；日志目录由 log.path 指定。文件上传大小限制改 spring.servlet.multipart.max-file-size 和 max-request-size。生成上传相关代码时引用 RuoyiConfig.getProfile() 取根路径。

### RuoYi 多模块 Maven 工程结构  `(p7)`

RuoYi 为多模块 Maven 工程：ruoyi-admin(启动入口/Web入口，含 RuoYiApplication 主类，聚合各模块依赖)、ruoyi-framework(框架核心：aspectj切面/config/datasource数据权限/interceptor/manager异步任务/shiro安全/web)、ruoyi-system(系统业务：domain/service/mapper/核心功能)、ruoyi-common(通用工具：annotation/config/constant/core/enums/exception/json/utils/xss)、ruoyi-generator(代码生成，可删)、ruoyi-quartz(定时任务，可删)。新增自定义业务模块命名 ruoyi-xxxxxx 并在 ruoyi-admin 的 pom.xml 加依赖。

### RuoYi common 包职责划分  `(p6)`

ruoyi-common 内部分包职责固定：annotation(自定义注解如@Excel/@Log/@DataScope/@DataSource/@RepeatSubmit/@Anonymous)、config(全局配置)、constant(常量Constants/HttpStatus等)、core(核心控制：controller.BaseController、domain.BaseEntity/AjaxResult、page.TableDataInfo/PageDomain、text)、enums(枚举如BusinessType/OperatorType/DataSourceType)、exception(自定义异常)、utils(工具类如StringUtils/SecurityUtils/ServletUtils)、xss(防XSS)。放置新代码时遵循此分包。

### RuoYi 业务模块包结构(controller/service/mapper/domain)  `(p7)`

业务模块标准包结构(以 ruoyi-system 为例)：controller(@RestController/@Controller)、service(接口层)、service.impl(@Service 实现)、mapper(@Mapper 接口)、domain(实体，继承 BaseEntity，带 @Excel)，对应 resources/mapper/**/*.xml(MyBatis 映射)。新增业务严格按此目录落代码。

### RuoYi 类与文件命名规范  `(p7)`

命名规范(以表 sys_student 为例)：实体 SysStudent.java(驼峰对应表名)、Mapper 接口 SysStudentMapper.java、Mapper XML SysStudentMapper.xml、Service 接口 ISysStudentService.java、Service 实现 SysStudentServiceImpl.java、Controller SysStudentController.java。主键/字段属性驼峰，对应列下划线，由 resultMap 映射。生成代码必须遵循此命名。

## 二、后端分层开发(Controller/Service/Mapper/Domain)

新增一个业务模块从实体到接口的标准写法——这是生成后端代码的核心依据。

### RuoYi 新增业务模块完整流程  `(p8)`

新增业务标准流程：1)设计建表(表名带前缀如 sys_，字段加 comment)；2)用【系统工具→代码生成】导入表，配置包名(默认com.ruoyi.system)/去前缀/作者，生成下载 ruoyi.zip；3)将生成的 Controller/Service/ISysXxxService/ServiceImpl/Mapper接口/Mapper.xml/domain 放入对应模块包；4)执行生成的菜单 SQL 配置权限。能用代码生成就不要手写脚手架。

### RuoYi Controller 继承 BaseController 与标准方法  `(p8)`

所有业务 Controller 继承 com.ruoyi.common.core.controller.BaseController，类上 @RestController(返回JSON) + @RequestMapping("/模块/资源")。继承得到的关键方法：startPage() 开启分页(PageHelper，仅对其后第一条 select 生效)、getDataTable(list) 把 List 包装成带 total/rows 的 TableDataInfo、toAjax(int rows) 把影响行数转成成功/失败 AjaxResult、getLoginUser()/getUserId()/getUsername() 取当前登录用户。

### RuoYi Controller 列表查询分页写法  `(p8)`

列表查询标准写法：@PreAuthorize("@ss.hasPermi('system:xxx:list')") + @GetMapping("/list")，方法内先 startPage() 再调 service 查 List，最后 return getDataTable(list)，返回类型 TableDataInfo。startPage() 自动读取请求参数 pageNum/pageSize/orderByColumn/isAsc 进行分页排序。务必 startPage() 紧贴查询语句之前，中间不要插入其它 select。

### RuoYi Controller 增删改方法与返回 AjaxResult  `(p8)`

增删改标准写法：新增 @PostMapping + add(@Validated @RequestBody SysXxx x)，修改 @PutMapping + edit(...)，删除 @DeleteMapping("/{ids}") + remove(@PathVariable Long[] ids)，详情 @GetMapping("/{id}")。方法体 return toAjax(service.insertXxx(x)) 或 return success(data)/AjaxResult.success(data)。统一返回 com.ruoyi.common.core.domain.AjaxResult，含 code(成功0/200，错误500)、msg、data。

### RuoYi 实体继承 BaseEntity 与公共字段  `(p7)`

业务实体(domain)统一继承 com.ruoyi.common.core.domain.BaseEntity，自动获得公共字段：createBy、createTime、updateBy、updateTime、remark(备注)，以及 params(Map，承载数据权限 dataScope/动态查询参数 beginTime/endTime)。实体只需声明业务字段，公共字段不重复定义。树形实体可继承 TreeEntity。

### RuoYi 实体 @Excel 注解导入导出  `(p6)`

实体字段加 @Excel 注解支持 Excel 导入导出。常用参数：name(列头)、sort(顺序，小在前)、dateFormat(如 yyyy-MM-dd)、dictType(关联字典自动转义如 sys_normal_disable)、readConverterExp(值映射如 "0=男,1=女")、cellType(Type.NUMERIC/STRING/IMAGE)、type(Type.ALL默认/EXPORT/IMPORT)、prompt、width。嵌套对象字段用 @Excels({@Excel(targetAttr="obj.f1"),...})。

### RuoYi Service 接口与 ServiceImpl 实现约定  `(p7)`

Service 分接口 + 实现：接口命名 ISysXxxService(I 前缀)，实现 SysXxxServiceImpl，类上加 @Service，通过 @Autowired 注入 Mapper。方法名约定 selectXxxList/selectXxxById/insertXxx/updateXxx/deleteXxxById/deleteXxxByIds。涉及多表写操作的方法加 @Transactional(rollbackFor = Exception.class)。业务异常直接抛出(如 throw new ServiceException(...))，不在 service 内 catch 吞掉。

### RuoYi Mapper 接口与 XML 映射约定  `(p7)`

数据访问层 Mapper 接口加 @Mapper，方法与 Service 对应(selectXxxList/selectXxxById/insertXxx/updateXxx/deleteXxxById/deleteXxxByIds)；对应 resources/mapper/模块/SysXxxMapper.xml 用 <resultMap> 映射列→属性、<sql id="selectXxxVo"> 抽取公共查询、<include refid> 引用。所有参数用 #{} 预编译防注入。数据权限查询在 where 末尾加 ${params.dataScope}。

### RuoYi 统一分页返回 TableDataInfo  `(p7)`

列表接口统一返回 com.ruoyi.common.core.page.TableDataInfo，字段：total(总记录数)、rows(数据列表)、code(状态码)、msg。由 BaseController.getDataTable(list) 构造，配合前端 bootstrap-table 的 server 端分页(读 rows/total)。不要自定义分页返回结构。

### RuoYi 参数校验 JSR-303 注解  `(p6)`

入参校验用 JSR-303：实体字段加 @NotBlank(message="...")、@NotNull、@Size(min=,max=,message=)、@Email、@Min/@Max、自定义 @Xss(防脚本)；Controller 方法参数加 @Validated 触发校验。校验失败由全局异常处理器统一转成 AjaxResult.error。新增/修改实体的必填与长度约束都应通过这些注解声明。

### RuoYi 全局异常处理 GlobalExceptionHandler  `(p6)`

全局异常由 @RestControllerAdvice 标注的 GlobalExceptionHandler 统一处理，用 @ExceptionHandler 捕获各类异常(业务异常 ServiceException、权限异常、参数校验异常 BindException/MethodArgumentNotValidException 等)并转成统一 AjaxResult.error(msg)。业务代码只管抛异常，不要每个 Controller 自己 try-catch 返回。

### RuoYi 操作日志注解 @Log  `(p7)`

需记录操作日志的写操作方法加 @Log(title = "业务名称", businessType = BusinessType.XXX)。BusinessType 枚举值：INSERT/UPDATE/DELETE/EXPORT/IMPORT/GRANT/FORCE/GENCODE/CLEAN/OTHER。框架通过 AOP 切面(LogAspect)自动把请求参数、返回、耗时、操作人写入 sys_oper_log。新增/修改/删除/导出/授权方法都应加 @Log。

### RuoYi 国际化 i18n 用法  `(p4)`

国际化资源放 i18n/messages_zh_CN.properties 与 messages_en_US.properties，代码中用 MessageUtils.message("key") 取值，@Excel 列头可用 ${key} 占位引用国际化。需要多语言的提示文案走此机制而非硬编码。

## 三、权限·安全·当前用户

RuoYi 的权限注解、数据权限、登录放行与取当前用户。

### RuoYi 权限注解 @PreAuthorize 与 hasPermi/hasRole  `(p8)`

接口权限用方法级注解 @PreAuthorize("@ss.hasPermi('模块:资源:操作')")，如 @ss.hasPermi('system:user:add')。权限标识三段式 module:resource:action(list/query/add/edit/remove/export/import)。角色判断用 @ss.hasRole('admin')；多权限 @ss.hasAnyPermi('a,b')。@ss 是 SecurityService Bean。(注：经典 Shiro 单体版亦可用 @RequiresPermissions("system:user:add")/@RequiresRoles("admin"))。生成代码每个 Controller 方法都要带对应权限注解。

### RuoYi 数据权限 @DataScope 用法  `(p7)`

数据权限用 @DataScope(deptAlias="d", userAlias="u") 注解在 service 查询方法上，AOP(DataScopeAspect)按当前用户角色的数据范围拼接 SQL 注入到 params.dataScope；Mapper XML 在 where 末尾加 ${params.dataScope} 生效。角色数据范围 5 种：1全部数据/2自定数据/3本部门/4本部门及以下/5仅本人。需要按部门/人隔离数据的列表查询都应加 @DataScope。

### RuoYi 匿名访问与登录放行(@Anonymous/anon)  `(p5)`

放行无需登录的接口：方法上加 @Anonymous 注解，或在 ShiroConfig 的 filterChainDefinitionMap 配置 url=anon。验证码开关 shiro.user.captchaEnabled，单账号在线数 shiro.session.maxSession。需要对外开放的接口用 @Anonymous，不要直接关闭全局鉴权。

### RuoYi 获取当前登录用户的标准方式  `(p6)`

取当前登录用户：单体版 ShiroUtils.getSysUser()/getUserId()/getLoginName()，或 PermissionUtils.getPrincipalProperty("userName")；分离版 SecurityUtils.getLoginUser()/getUserId()/getUsername()。模板 ${@permission.getPrincipalProperty('userName')}，JS [[${@permission.getPrincipalProperty('userName')}]]。业务里需要操作人/userId 用这些工具，不要从前端传。

### RuoYi CORS 跨域与 XSS 白名单配置  `(p4)`

跨域：方法 @CrossOrigin 或全局重写 addCorsMappings 或过滤器。XSS 过滤误杀富文本：application.yml 配 xss.excludes=/system/notice/* 白名单放行特定URL。Long 大数前端精度丢失：字段加 @JsonFormat 或序列化为 String。这些是生成富文本/跨域接口时的标准处理。

## 四、前端 Thymeleaf 与请求契约

单体版前端(Thymeleaf+jQuery)页面写法与前后端请求/响应契约。

### RuoYi 前后端 URL 与请求契约  `(p6)`

前端通过 $.operate/$.table 调后端，标准 URL：list(列表分页 POST/GET)、add(GET返回表单页+POST提交)、edit、remove(删除)、export、importData、importTemplate、detail。Controller 的 @RequestMapping 前缀 + 上述子路径需与前端 table.options 的 createUrl/updateUrl/removeUrl/exportUrl/importUrl/detailUrl 对齐，否则 404。生成前后端代码时务必保持 URL 一致。

### RuoYi 前端响应码契约(web_status)  `(p6)`

前端按 result.code 判断：SUCCESS(0/200)、WARNING、ERROR(500)；统一读 result.msg 提示、result.data 取数据、列表读 result.rows/result.total。后端 AjaxResult 必须遵循该 code/msg/data 契约，TableDataInfo 必须含 rows/total，前端才能正确渲染。

### RuoYi 前端按钮权限与后端 hasPermi 映射  `(p6)`

前端按钮显隐由权限字符串控制：模板 shiro:hasPermission="system:user:add"，JS 中 [[${@permission.hasPermi('system:user:add')}]]。这些权限字符串必须与后端 @PreAuthorize("@ss.hasPermi('system:user:add')") / 菜单表 perms 字段完全一致。生成菜单 SQL 时按 module:resource:action 三段式定义 perms。

### RuoYi 弹窗组件 $.modal.open 约定  `(p5)`

弹窗统一用 $.modal.open(title, url, width, height, callback)，底层 layer.open(type:2 iframe)。url 指向后端返回表单页的 @GetMapping(如 /add、/edit/{id})。新增/修改/详情都通过弹窗加载子页面，子页面 submitHandler 提交。生成的表单页 Controller 方法返回 prefix + "/add" 之类的视图名。

### RuoYi 新增/修改弹窗与后端表单页方法  `(p5)`

新增 $.operate.add(id) 打开 createUrl，后端 @GetMapping("/add") 返回表单视图(带id则 @GetMapping("/add/{xxId}") 预查数据塞 ModelMap)；修改 $.operate.edit(id) 打开 updateUrl，后端 @GetMapping("/edit/{xxId}") 把 service.selectXxxById 结果放 ModelMap 返回 edit 视图。优先级：传参ID→uniqueId列→首列。生成单体版表单页 Controller 遵循此模式。

### RuoYi 删除/批量删除前后端约定  `(p5)`

单条删除 $.operate.remove(id) 弹确认框后 POST，后端 @PostMapping("/remove") + @ResponseBody，参数 String ids，return toAjax(service.deleteXxxByIds(ids))；批量删除 $.operate.removeAll() 收集勾选行(uniqueId 或首列)拼成逗号串提交同一 /remove。删除方法务必加确认与权限+@Log(businessType=DELETE)。

### RuoYi 提交保存 $.operate.save 与后端 addSave/editSave  `(p5)`

表单提交用 $.operate.save(url, $('#form-xxx').serialize(), callback)，POST + json。后端新增提交 @PostMapping("/add") addSave(@Validated Xxx x) return toAjax(insertXxx)，修改提交 @PostMapping("/edit") editSave(@Validated Xxx x) return toAjax(updateXxx)。提交期间前端 $.modal.disable() 防重复点击。

### RuoYi 搜索/导入/导出/下载模板后端方法  `(p5)`

搜索 $.table.search 刷新 bootstrap-table 触发 @PostMapping("/list") startPage()+getDataTable；导入 @PostMapping("/importData") importData(MultipartFile file, boolean updateSupport) 用 ExcelUtil.importExcel(file.getInputStream())；下载模板 @GetMapping("/importTemplate") 用 ExcelUtil.importTemplateExcel；导出 @PostMapping("/export") 用 ExcelUtil.exportExcel(list,"xx数据")。导入导出实体字段需带 @Excel。

### RuoYi Thymeleaf 模板引擎约定  `(p4)`

单体版视图用 Thymeleaf。常用属性：th:text(转义输出)、th:utext(不转义)、th:if/th:unless、th:each、th:href="@{/path(param=${v})}"、th:object/*{prop}、th:replace/th:insert 引入片段(th:fragment 定义)。内联：[[${var}]] 转义、[(${var})] 不转义，th:inline="javascript" 在 JS 中取后端变量。生成单体版页面遵循此语法。

### RuoYi 模板内字典与权限标签  `(p4)`

模板中取字典 <dict:select dict="sys_normal_disable"> 或 JS 内 [[${@dict.getType('sys_normal_disable')}]]；取权限 [[${@permission.hasPermi('system:user:add')}]]；取当前用户 [[${@permission.getPrincipalProperty('userName')}]]；日期格式化 [[${#dates.format(date,'yyyy-MM-dd HH:mm:ss')}]]。生成模板页用这些内置对象而非手写逻辑。

### RuoYi 前端组件库清单  `(p4)`

单体版前端集成组件：bootstrap-table(数据表格,server分页,formatter)、layer(弹窗 alert/confirm/msg/open)、jquery-validate(表单校验)、bootstrap-datetimepicker/laydate(日期时间)、bootstrap-select/select2(下拉,select2支持ajax data {id,text})、bootstrap-fileinput/jasny-bootstrap(文件上传)、bootstrap-duallistbox(双列表)、summernote/ueditor(富文本)、jquery-cxselect(级联下拉)、x-editable(行内编辑)、bootstrap-suggest/typeahead(自动补全)、icheck。生成单体版页面优先用这些既有组件。

## 五、通用能力(字典/缓存/防重/上传/多数据源)

跨模块复用的通用机制。

### RuoYi 数据字典使用约定(dict)  `(p7)`

数据字典分 sys_dict_type(类型) 与 sys_dict_data(数据项 dictLabel/dictValue)。后端在模板用 [[${@dict.getType('sys_normal_disable')}]] 取字典；实体 @Excel(dictType="sys_xxx") 导出自动转标签。前端表格 formatter 用 $.table.selectDictLabel(datas, value) 显示标签。状态/性别/是否等枚举类字段一律用字典(如 sys_normal_disable 0正常1停用、sys_user_sex)，不要硬编码。

### RuoYi 缓存与 Redis 用法  `(p6)`

缓存基于 Redis：通用缓存操作走 RedisCache(setCacheObject/getCacheObject/deleteObject/setCacheList 等)注入使用；字典、参数配置等热点数据启动时载入缓存，CacheUtils/字典缓存框架自动维护。集群会话可用 shiro-redis 替换本地 ehcache。需要缓存的数据用 RedisCache 而非自建 Map。

### RuoYi 防重复提交 @RepeatSubmit  `(p6)`

防止重复提交：后端写操作方法加 @RepeatSubmit 注解(基于 RepeatSubmitInterceptor，同一用户短时间相同参数请求被拦截)；前端配合 $.modal.disable()/$.modal.enable() 在提交期间禁用按钮。新增/修改等关键写接口建议加 @RepeatSubmit。

### RuoYi 文件上传下载工具类  `(p5)`

文件上传用 FileUploadUtils.upload(基础路径, MultipartFile)，根路径取 RuoyiConfig.getProfile()；下载走 common/download?fileName=xxx&delete=true 通用接口。上传文件通过 /profile/** 映射对外访问。文件相关功能复用这些工具与通用接口，不要各自实现。

### RuoYi 多数据源插件集成(@DataSource)  `(p6)`

多数据源在 application-druid.yml 启用 slave，方法上加 @DataSource(value=DataSourceType.SLAVE) 切换(DataSourceType 枚举 MASTER/SLAVE)，由 DataSourceAspect 动态路由。支持多库类型并存(MySQL/Oracle/PostgreSQL,删 driverClassName 自动识别)。分布式事务可集成 atomikos/sharding-jdbc。需要从库或多库的查询用 @DataSource 注解。

### RuoYi 常见插件集成选项  `(p4)`

RuoYi 可集成插件：Swagger/SpringFox(接口文档,swagger.enable 开关)、knife4j(增强Swagger,/doc.html)、Druid(数据监控)、Redis集群会话(shiro-redis)、JWT(前后端分离鉴权)、CAS(单点登录)、Docker(一键部署)、PostgreSQL(改pagehelper dialect)、MyBatis-Plus(CRUD增强,MybatisPlusInterceptor)、EasyExcel(@ExcelProperty)、MinIO(对象存储)、WebSocket、aj-captcha(滑块验证码)、JustAuth(第三方登录)、Undertow替代Tomcat。按需集成，遵循各插件官方依赖版本。

### RuoYi PostgreSQL/数据库适配约定  `(p4)`

切换数据库类型时：改 pagehelper.helperDialect(如 postgresql)或开 autoRuntimeDialect 自动识别、改驱动(org.postgresql.Driver)、注意 SQL 方言差异(MySQL sysdate() → PostgreSQL now())。Mapper XML 中的数据库函数需按目标库改写。为非 MySQL 库生成 Mapper 时注意方言。

### RuoYi 定时任务与 quartz 约定  `(p5)`

定时任务基于 Quartz：任务类注册为 @Component("ryTask")，方法形如 ryParams(String params)，调用表达式 ryTask.ryParams('值') 或全类名 com.ruoyi.quartz.task.RyTask.ryParams('值')。分布式多机连同库会随机执行,单机执行可注释 ScheduleConfig。新增定时任务按此注册 Bean 与调用目标字符串。

## 六、代码生成·扩展·运维 FAQ

代码生成器、项目扩展与高频排查项。

### RuoYi 项目扩展与多模块接入方式  `(p6)`

扩展业务时新增 ruoyi-xxx 模块：在父 pom 注册 module，ruoyi-admin 的 pom 加依赖，启动类 @SpringBootApplication(scanBasePackages={"com.ruoyi","com.test"}) 或 @ComponentScan + @MapperScan 扫描到新包。修改根包名 com.ruoyi 需同步改目录/pom/@MapperScan/@Pointcut/Constants/typeAliasesPackage/XML namespace/日志配置。新增模块不要漏配扫描与依赖，否则 404/Bean 找不到。

### RuoYi 代码生成支持的表类型(单表/树表/主子表)  `(p5)`

代码生成器支持三种生成模板：单表(CRUD)、树表(树形结构,需 parentId/ancestors 字段,继承 TreeEntity)、主子表(主表+明细子表,一对多)。代码生成页可配置生成方式、上级菜单、字典关联、查询/列表/必填字段等。优先用代码生成器产出符合规范的脚手架再改业务，不要手搓树表/主子表样板代码。

### RuoYi 业务模块 404 与无权限排查  `(p6)`

业务模块 404/无权限常见原因：1)菜单未配置给该用户;2)角色未授该菜单权限;3)菜单URL与后端@RequestMapping不一致;4)多模块未在ruoyi-admin加pom依赖或启动类未扫描到包;5)权限标识(perms/@ss.hasPermi)与代码不匹配。生成业务必须同时产出匹配的菜单SQL与权限标识。

### RuoYi 代码生成不显示新表的处理  `(p5)`

代码生成页看不到新建的表：默认要求表有注释(comment)。解决：建表时给表和字段加 comment；或 4.0+ 版本在代码生成页直接"导入表"。生成前确保表结构带注释，字段 comment 会成为生成代码的字段名/注释/列头。

### RuoYi 导出 PDF/水印/嵌套字段技巧  `(p4)`

导出扩展：导PDF加 itextpdf+itext-asian 依赖,仿 ExcelUtil 写 ExcelPDFUtil.exportPDF;Excel加水印用 ooxml-schemas + ExcelWaterMark.insertWaterMarkText;导出子对象多字段用 @Excels({@Excel(targetAttr="obj.f1"),...})。常规导出直接用 ExcelUtil + @Excel 即可，特殊需求才扩展。

### RuoYi Swagger 接口文档约定  `(p4)`

接口文档用 Swagger：swagger.enable 控制开关(生产关闭)。Controller/方法可加 @Api/@ApiOperation/@ApiModelProperty 描述;BaseEntity 的 params 字段建议 @ApiModelProperty(hidden=true) 避免转换报错。可换 knife4j 用 /doc.html。生成对外 API 时补充 Swagger 注解便于联调。

### RuoYi 版本演进与升级约定  `(p4)`

RuoYi 持续迭代(截至文档 v4.8.x)，更新主要为依赖升级(spring-boot/shiro/poi/bootstrap)、安全修复(Thymeleaf SSTI RCE/Log4j RCE/XSS/SQL注入)、功能增强与代码生成改进。生成代码时优先匹配项目当前依赖版本,安全相关写法(模板变量转义、预编译SQL)必须遵循最新修复后的规范,不要复刻已知漏洞写法。
