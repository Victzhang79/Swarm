package com.ruoyi.alarm.engine.service.impl;

import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import com.ruoyi.alarm.engine.service.INotifyService;
import com.ruoyi.common.utils.StringUtils;
import com.ruoyi.common.utils.http.HttpUtils;

/**
 * VoIP通知渠道实现
 *
 * @author ruoyi
 */
@Service
public class VoipNotifyServiceImpl implements INotifyService
{
    private static final Logger log = LoggerFactory.getLogger(VoipNotifyServiceImpl.class);

    @Override
    public String getChannelType()
    {
        return "voip";
    }

    @Override
    public void sendNotify(Map<String, Object> context)
    {
        String voipId = castToString(context.get("voipId"));
        String content = castToString(context.get("content"));

        if (StringUtils.isEmpty(voipId) || StringUtils.isEmpty(content))
        {
            log.warn("Voip sendNotify failed, voipId or content is empty");
            return;
        }

        try
        {
            // 通过VoIP网关发起语音通话
            String apiUrl = castToString(context.get("voipApiUrl"));
            if (StringUtils.isEmpty(apiUrl))
            {
                log.warn("Voip sendNotify failed, voipApiUrl is empty");
                return;
            }

            String response = HttpUtils.sendPost(apiUrl, buildRequestBody(voipId, content));
            log.info("Voip sendNotify response: {}", response);
        }
        catch (Exception e)
        {
            log.error("Voip sendNotify error", e);
        }
    }

    @Override
    public void sendRecover(Map<String, Object> context)
    {
        // 恢复提醒跳过VoIP渠道
        log.info("Voip sendRecover skipped for voip channel");
    }

    @Override
    public void onCallback(Map<String, Object> callback)
    {
        log.info("Voip onCallback: {}", callback);
    }

    private String buildRequestBody(String voipId, String content)
    {
        return "voipId=" + voipId + "&content=" + content;
    }

    private String castToString(Object obj)
    {
        return obj != null ? obj.toString() : null;
    }
}
