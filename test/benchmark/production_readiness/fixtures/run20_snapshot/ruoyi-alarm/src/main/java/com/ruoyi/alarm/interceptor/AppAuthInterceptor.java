package com.ruoyi.alarm.interceptor;

import java.util.Date;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.HandlerInterceptor;
import com.ruoyi.alarm.domain.AlarmApp;
import com.ruoyi.alarm.service.IAlarmAppService;
import com.ruoyi.common.core.domain.AjaxResult;
import com.ruoyi.common.utils.StringUtils;
import com.ruoyi.common.utils.http.HttpUtils;

/**
 * 应用鉴权拦截器
 * 从请求头 X-App-Id/X-App-Secret 提取凭证，查询 alarm_app 表校验 status 与 expire_time
 * 校验通过后将 AppContext(appId/appName) 存入 ThreadLocal 供后续链路使用
 * 校验失败返回 HTTP 401 JSON 错误体
 * 
 * @author ruoyi
 */
@Component
public class AppAuthInterceptor implements HandlerInterceptor
{
    private static final String HEADER_APP_ID = "X-App-Id";
    private static final String HEADER_APP_SECRET = "X-App-Secret";

    @Autowired
    private IAlarmAppService alarmAppService;

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler)
    {
        String appIdStr = request.getHeader(HEADER_APP_ID);
        String appSecret = request.getHeader(HEADER_APP_SECRET);

        // 校验请求头参数
        if (StringUtils.isEmpty(appIdStr) || StringUtils.isEmpty(appSecret))
        {
            response.setStatus(HttpStatus.UNAUTHORIZED);
            response.setContentType("application/json;charset=UTF-8");
            try
            {
                response.getWriter().write(HttpUtils.toJsonString(AjaxResult.error(401, "缺少应用凭证")));
            }
            catch (Exception e)
            {
                return false;
            }
            return false;
        }

        // 解析 appId
        Long appId;
        try
        {
            appId = Long.parseLong(appIdStr);
        }
        catch (NumberFormatException e)
        {
            response.setStatus(HttpStatus.UNAUTHORIZED);
            response.setContentType("application/json;charset=UTF-8");
            try
            {
                response.getWriter().write(HttpUtils.toJsonString(AjaxResult.error(401, "appId格式错误")));
            }
            catch (Exception ex)
            {
                return false;
            }
            return false;
        }

        // 查询应用信息
        AlarmApp alarmApp = alarmAppService.selectAlarmAppByAppId(appId);
        if (alarmApp == null)
        {
            response.setStatus(HttpStatus.UNAUTHORIZED);
            response.setContentType("application/json;charset=UTF-8");
            try
            {
                response.getWriter().write(HttpUtils.toJsonString(AjaxResult.error(401, "应用不存在")));
            }
            catch (Exception e)
            {
                return false;
            }
            return false;
        }

        // 校验状态
        if (!"0".equals(alarmApp.getStatus()))
        {
            response.setStatus(HttpStatus.UNAUTHORIZED);
            response.setContentType("application/json;charset=UTF-8");
            try
            {
                response.getWriter().write(HttpUtils.toJsonString(AjaxResult.error(401, "应用已禁用")));
            }
            catch (Exception e)
            {
                return false;
            }
            return false;
        }

        // 校验过期时间
        if (alarmApp.getExpireTime() != null && alarmApp.getExpireTime().before(new Date()))
        {
            response.setStatus(HttpStatus.UNAUTHORIZED);
            response.setContentType("application/json;charset=UTF-8");
            try
            {
                response.getWriter().write(HttpUtils.toJsonString(AjaxResult.error(401, "应用已过期")));
            }
            catch (Exception e)
            {
                return false;
            }
            return false;
        }

        // 校验 appSecret
        if (!alarmApp.getAppSecret().equals(appSecret))
        {
            response.setStatus(HttpStatus.UNAUTHORIZED);
            response.setContentType("application/json;charset=UTF-8");
            try
            {
                response.getWriter().write(HttpUtils.toJsonString(AjaxResult.error(401, "应用密钥错误")));
            }
            catch (Exception e)
            {
                return false;
            }
            return false;
        }

        // 鉴权通过，设置上下文
        AppContext appContext = new AppContext(appId, alarmApp.getAppName());
        AppContext.setContext(appContext);

        return true;
    }

    /**
     * 应用上下文
     */
    public static class AppContext
    {
        private static final ThreadLocal<AppContext> CONTEXT_HOLDER = new ThreadLocal<>();

        private Long appId;
        private String appName;

        public AppContext()
        {
        }

        public AppContext(Long appId, String appName)
        {
            this.appId = appId;
            this.appName = appName;
        }

        public Long getAppId()
        {
            return appId;
        }

        public void setAppId(Long appId)
        {
            this.appId = appId;
        }

        public String getAppName()
        {
            return appName;
        }

        public void setAppName(String appName)
        {
            this.appName = appName;
        }

        /**
         * 设置上下文到当前线程
         */
        public static void setContext(AppContext context)
        {
            CONTEXT_HOLDER.set(context);
        }

        /**
         * 获取当前线程的上下文
         */
        public static AppContext getContext()
        {
            return CONTEXT_HOLDER.get();
        }

        /**
         * 清除当前线程的上下文
         */
        public static void clearContext()
        {
            CONTEXT_HOLDER.remove();
        }
    }
}
