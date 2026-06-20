package com.ruoyi.alarm.engine.service.impl;

import java.util.HashMap;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ruoyi.alarm.engine.service.INotifyService;
import com.ruoyi.common.utils.StringUtils;
import com.ruoyi.common.utils.http.HttpUtils;

/**
 * 企业微信通知渠道实现
 *
 * @author ruoyi
 */
@Service
public class WechatWorkNotifyServiceImpl implements INotifyService
{
    private static final Logger log = LoggerFactory.getLogger(WechatWorkNotifyServiceImpl.class);
    private static final ObjectMapper objectMapper = new ObjectMapper();

    @Override
    public String getChannelType()
    {
        return "wechat_work";
    }

    @Override
    public void sendNotify(Map<String, Object> context)
    {
        String webhookUrl = castToString(context.get("webhookUrl"));
        String content = castToString(context.get("content"));
        String mentionedList = castToString(context.get("mentionedList"));

        if (StringUtils.isEmpty(webhookUrl) || StringUtils.isEmpty(content))
        {
            log.warn("WechatWork sendNotify failed, webhookUrl or content is empty");
            return;
        }

        try
        {
            Map<String, Object> body = new HashMap<>();
            body.put("msgtype", "markdown");
            Map<String, Object> markdown = new HashMap<>();
            markdown.put("content", content);
            body.put("markdown", markdown);

            if (StringUtils.isNotEmpty(mentionedList))
            {
                body.put("mentioned_list", StringUtils.str2List(mentionedList, ","));
            }

            String response = HttpUtils.sendPost(webhookUrl, objectMapper.writeValueAsString(body));
            log.info("WechatWork sendNotify response: {}", response);
        }
        catch (Exception e)
        {
            log.error("WechatWork sendNotify error", e);
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
        log.info("WechatWork onCallback: {}", callback);
    }

    private String castToString(Object obj)
    {
        return obj != null ? obj.toString() : null;
    }
}
