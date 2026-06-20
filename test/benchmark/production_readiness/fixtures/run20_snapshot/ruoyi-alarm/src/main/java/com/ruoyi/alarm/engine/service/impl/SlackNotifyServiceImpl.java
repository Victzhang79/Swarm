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
 * Slack 通知渠道实现
 *
 * @author ruoyi
 */
@Service
public class SlackNotifyServiceImpl implements INotifyService
{
    private static final Logger log = LoggerFactory.getLogger(SlackNotifyServiceImpl.class);
    private static final ObjectMapper objectMapper = new ObjectMapper();

    @Override
    public String getChannelType()
    {
        return "slack";
    }

    @Override
    public void sendNotify(Map<String, Object> context)
    {
        String webhookUrl = castToString(context.get("webhookUrl"));
        String content = castToString(context.get("content"));

        if (StringUtils.isEmpty(webhookUrl) || StringUtils.isEmpty(content))
        {
            log.warn("Slack sendNotify failed, webhookUrl or content is empty");
            return;
        }

        try
        {
            Map<String, Object> body = new HashMap<>();
            body.put("text", content);

            Map<String, Object> block = new HashMap<>();
            block.put("type", "section");
            block.put("text", new HashMap<String, Object>() {{
                put("type", "mrkdwn");
                put("text", content);
            }});

            Map<String, Object> divider = new HashMap<>();
            divider.put("type", "divider");

            body.put("blocks", new Object[]{block, divider});

            String response = HttpUtils.sendPost(webhookUrl, objectMapper.writeValueAsString(body));
            log.info("Slack sendNotify response: {}", response);
        }
        catch (Exception e)
        {
            log.error("Slack sendNotify error", e);
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
        log.info("Slack onCallback: {}", callback);
    }

    private String castToString(Object obj)
    {
        return obj != null ? obj.toString() : null;
    }
}
