package com.ruoyi.alarm.engine.service.impl;

import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import com.ruoyi.alarm.engine.service.IdempotentService;
import com.ruoyi.alarm.engine.service.INotifyService;
import com.ruoyi.common.utils.StringUtils;
import com.ruoyi.common.utils.http.HttpUtils;

/**
 * 站内消息通知渠道实现
 *
 * @author ruoyi
 */
@Service
public class InnerPushNotifyServiceImpl implements INotifyService
{
    private static final Logger log = LoggerFactory.getLogger(InnerPushNotifyServiceImpl.class);

    @Autowired
    private IdempotentService idempotentService;

    @Override
    public String getChannelType()
    {
        return "inner_push";
    }

    @Override
    public void sendNotify(Map<String, Object> context)
    {
        String innerPushId = castToString(context.get("innerPushId"));
        String content = castToString(context.get("content"));

        if (StringUtils.isEmpty(innerPushId) || StringUtils.isEmpty(content))
        {
            log.warn("InnerPush sendNotify failed, innerPushId or content is empty");
            return;
        }

        try
        {
            // 向站内消息网关推送消息
            String apiUrl = castToString(context.get("innerPushApiUrl"));
            if (StringUtils.isEmpty(apiUrl))
            {
                log.warn("InnerPush sendNotify failed, innerPushApiUrl is empty");
                return;
            }

            String response = HttpUtils.sendPost(apiUrl, buildRequestBody(innerPushId, content));
            log.info("InnerPush sendNotify response: {}", response);
        }
        catch (Exception e)
        {
            log.error("InnerPush sendNotify error", e);
        }
    }

    @Override
    public void sendRecover(Map<String, Object> context)
    {
        sendNotify(context);
    }

    @Override
    public void onCallback(Map<String, Object> callback)
    {
        String idempotentKey = castToString(callback.get("idempotentKey"));
        Long ignoreInterval = castToLong(callback.get("ignoreInterval"));

        if (StringUtils.isEmpty(idempotentKey) || ignoreInterval == null)
        {
            log.warn("InnerPush onCallback failed, idempotentKey or ignoreInterval is empty");
            return;
        }

        // 标记为免提醒状态
        idempotentService.markIgnored(idempotentKey, ignoreInterval);
        log.info("InnerPush onCallback marked ignored for idempotentKey: {}", idempotentKey);
    }

    private String buildRequestBody(String innerPushId, String content)
    {
        return "innerPushId=" + innerPushId + "&content=" + content;
    }

    private String castToString(Object obj)
    {
        return obj != null ? obj.toString() : null;
    }

    private Long castToLong(Object obj)
    {
        if (obj == null)
        {
            return null;
        }
        try
        {
            return Long.parseLong(obj.toString());
        }
        catch (NumberFormatException e)
        {
            return null;
        }
    }
}
