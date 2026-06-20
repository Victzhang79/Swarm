package com.ruoyi.alarm.controller;

import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import com.ruoyi.alarm.dto.CallbackRequest;
import com.ruoyi.alarm.dto.NotifyResponse;
import com.ruoyi.alarm.service.IAlarmEngineService;

import jakarta.annotation.Resource;

/**
 * 预警回调控制器
 *
 * @author ruoyi
 */
@RestController
@RequestMapping("/notify/callback")
public class NotifyCallbackController
{
    @Resource
    private IAlarmEngineService alarmEngineService;

    /**
     * 处理消息按钮回调
     *
     * @param request 回调请求
     * @return 响应结果
     */
    @PostMapping
    public NotifyResponse callback(CallbackRequest request)
    {
        try
        {
            alarmEngineService.processCallback(request.getRecordId(), request.getCallbackType());
            return NotifyResponse.success();
        }
        catch (Exception e)
        {
            return NotifyResponse.error(500, "回调处理失败: " + e.getMessage());
        }
    }
}
