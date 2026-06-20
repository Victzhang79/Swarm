package com.ruoyi.alarm.controller;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import com.ruoyi.alarm.domain.dto.NotifyRequestDTO;
import com.ruoyi.alarm.service.INotifyApiService;

/**
 * 对外预警API控制器
 * 提供简单预警、组合预警、恢复、删除、VoIP推送、回调等接口
 * 不走Shiro鉴权，使用AppAuthInterceptor基于appId+appSecret校验
 * 
 * @author ruoyi
 */
@RestController
@RequestMapping("/notify")
public class NotifyApiController
{
    @Autowired
    private INotifyApiService notifyApiService;

    /**
     * 简单预警发送
     * 
     * @param request 简单预警请求
     * @return 响应结果
     */
    @PostMapping("/simple")
    public NotifyRequestDTO.NotifyResponse simple(@RequestBody NotifyRequestDTO.NotifySimpleRequest request)
    {
        return notifyApiService.processSimpleNotify(request);
    }

    /**
     * 组合预警发送
     * 
     * @param request 组合预警请求
     * @return 响应结果
     */
    @PostMapping("/compose")
    public NotifyRequestDTO.NotifyResponse compose(@RequestBody NotifyRequestDTO.NotifyComposeRequest request)
    {
        return notifyApiService.processComposeNotify(request);
    }

    /**
     * 恢复提醒
     * 
     * @param request 恢复请求
     * @return 响应结果
     */
    @PostMapping("/recover")
    public NotifyRequestDTO.NotifyResponse recover(@RequestBody NotifyRequestDTO.NotifyRecoverRequest request)
    {
        return notifyApiService.processRecover(request);
    }

    /**
     * 预警删除
     * 
     * @param request 删除请求
     * @return 响应结果
     */
    @PostMapping("/delete")
    public NotifyRequestDTO.NotifyResponse deleteNotify(@RequestBody NotifyRequestDTO.NotifyDeleteRequest request)
    {
        return notifyApiService.processDelete(request);
    }

    /**
     * VoIP推送发送
     * 
     * @param request VoIP发送请求
     * @return 响应结果
     */
    @PostMapping("/apns/send_voip")
    public NotifyRequestDTO.NotifyResponse sendVoip(@RequestBody NotifyRequestDTO.VoipSendRequest request)
    {
        return notifyApiService.processVoipNotify(request);
    }

    /**
     * 消息按钮回调
     * 
     * @param request 回调请求
     * @return 响应结果
     */
    @PostMapping("/callback")
    public NotifyRequestDTO.NotifyResponse callback(@RequestBody NotifyRequestDTO.CallbackRequest request)
    {
        return notifyApiService.processCallback(request);
    }
}
