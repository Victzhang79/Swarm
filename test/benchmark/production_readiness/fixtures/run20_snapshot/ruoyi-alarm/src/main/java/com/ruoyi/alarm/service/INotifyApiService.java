package com.ruoyi.alarm.service;

import com.ruoyi.alarm.domain.dto.NotifyRequestDTO;

/**
 * 对外预警API服务接口
 * 
 * @author ruoyi
 */
public interface INotifyApiService
{
    /**
     * 处理简单预警发送
     * 
     * @param request 简单预警请求
     * @return 响应结果
     */
    NotifyRequestDTO.NotifyResponse processSimpleNotify(NotifyRequestDTO.NotifySimpleRequest request);

    /**
     * 处理组合预警发送
     * 
     * @param request 组合预警请求
     * @return 响应结果
     */
    NotifyRequestDTO.NotifyResponse processComposeNotify(NotifyRequestDTO.NotifyComposeRequest request);

    /**
     * 处理恢复提醒
     * 
     * @param request 恢复请求
     * @return 响应结果
     */
    NotifyRequestDTO.NotifyResponse processRecover(NotifyRequestDTO.NotifyRecoverRequest request);

    /**
     * 处理预警删除
     * 
     * @param request 删除请求
     * @return 响应结果
     */
    NotifyRequestDTO.NotifyResponse processDelete(NotifyRequestDTO.NotifyDeleteRequest request);

    /**
     * 处理VoIP推送发送
     * 
     * @param request VoIP发送请求
     * @return 响应结果
     */
    NotifyRequestDTO.NotifyResponse processVoipNotify(NotifyRequestDTO.VoipSendRequest request);

    /**
     * 处理消息按钮回调
     * 
     * @param request 回调请求
     * @return 响应结果
     */
    NotifyRequestDTO.NotifyResponse processCallback(NotifyRequestDTO.CallbackRequest request);
}
