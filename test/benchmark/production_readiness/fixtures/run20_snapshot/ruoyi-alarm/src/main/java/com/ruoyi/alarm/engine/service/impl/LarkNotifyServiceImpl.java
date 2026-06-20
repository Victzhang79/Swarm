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
 * 飞书通知渠道实现
 *
 * @author ruoyi
 */
@Service
public class LarkNotifyServiceImpl implements INotifyService
{
    private static final Logger log = LoggerFactory.getLogger(LarkNotifyServiceImpl.class);
    private static final ObjectMapper objectMapper = new ObjectMapper();

    @Override
    public String getChannelType()
    {
        return "lark";
    }

    @Override
    public void sendNotify(Map<String, Object> context)
    {
        String webhookUrl = castToString(context.get("webhookUrl"));
        String content = castToString(context.get("content"));

        if (StringUtils.isEmpty(webhookUrl) || StringUtils.isEmpty(content))
        {
            log.warn("Lark sendNotify failed, webhookUrl or content is empty");
            return;
        }

        try
        {
            Map<String, Object> body = new HashMap<>();
            body.put("msg_type", "interactive");
            body.put("card", buildCard(content));

            String response = HttpUtils.sendPost(webhookUrl, objectMapper.writeValueAsString(body));
            log.info("Lark sendNotify response: {}", response);
        }
        catch (Exception e)
        {
            log.error("Lark sendNotify error", e);
        }
    }

    private Map<String, Object> buildCard(String content)
    {
        Map<String, Object> card = new HashMap<>();
        card.put("header", buildHeader());
        card.put("elements", buildElements(content));
        card.put("buttons", buildButtons());
        return card;
    }

    private Map<String, Object> buildHeader()
    {
        Map<String, Object> header = new HashMap<>();
        header.put("title", "预警通知");
        header.put("template", "red");
        return header;
    }

    private Object[] buildElements(String content)
    {
        Map<String, Object> element = new HashMap<>();
        element.put("tag", "markdown");
        element.put("content", content);
        return new Object[]{element};
    }

    private Object[] buildButtons()
    {
        Map<String, Object> button = new HashMap<>();
        button.put("tag", "button");
        button.put("text", new HashMap<String, Object>() {{
            put("tag", "plain_text");
            put("content", "忽略");
        }});
        button.put("type", "primary");
        button.put("value", new HashMap<String, Object>() {{
            put("callback_type", 1001);
        }});
        return new Object[]{button};
    }

    @Override
    public void sendRecover(Map<String, Object> context)
    {
        sendNotify(context);
    }

    @Override
    public void onCallback(Map<String, Object> callback)
    {
        log.info("Lark onCallback: {}", callback);
    }

    private String castToString(Object obj)
    {
        return obj != null ? obj.toString() : null;
    }
}
