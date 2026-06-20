package com.ruoyi.alarm.engine.service.impl;

import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import com.ruoyi.alarm.engine.service.INotifyService;
import com.ruoyi.common.utils.StringUtils;
import com.ruoyi.common.utils.http.HttpUtils;

/**
 * 电话通知渠道实现
 *
 * @author ruoyi
 */
@Service
public class VoiceNotifyServiceImpl implements INotifyService
{
    private static final Logger log = LoggerFactory.getLogger(VoiceNotifyServiceImpl.class);

    @Override
    public String getChannelType()
    {
        return "voice";
    }

    @Override
    public void sendNotify(Map<String, Object> context)
    {
        String phone = castToString(context.get("phone"));
        String content = castToString(context.get("content"));

        if (StringUtils.isEmpty(phone) || StringUtils.isEmpty(content))
        {
            log.warn("Voice sendNotify failed, phone or content is empty");
            return;
        }

        try
        {
            // 通过电话API拨打通知
            String apiUrl = castToString(context.get("voiceApiUrl"));
            if (StringUtils.isEmpty(apiUrl))
            {
                log.warn("Voice sendNotify failed, voiceApiUrl is empty");
                return;
            }

            String response = HttpUtils.sendPost(apiUrl, buildRequestBody(phone, content));
            log.info("Voice sendNotify response: {}", response);
        }
        catch (Exception e)
        {
            log.error("Voice sendNotify error", e);
        }
    }

    @Override
    public void sendRecover(Map<String, Object> context)
    {
        // 恢复提醒跳过语音渠道
        log.info("Voice sendRecover skipped for voice channel");
    }

    @Override
    public void onCallback(Map<String, Object> callback)
    {
        log.info("Voice onCallback: {}", callback);
    }

    private String buildRequestBody(String phone, String content)
    {
        return "phone=" + phone + "&content=" + content;
    }

    private String castToString(Object obj)
    {
        return obj != null ? obj.toString() : null;
    }
}
