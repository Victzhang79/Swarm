package com.ruoyi.alarm.engine.service;

import java.util.Map;

/**
 * 预警引擎调度服务
 *
 * @author ruoyi
 */
public interface AlarmEngineService
{
    /**
     * 异步分发预警通知
     *
     * @param taskConfig 任务配置
     * @param variables 变量映射
     */
    void dispatch(Map<String, Object> taskConfig, Map<String, String> variables);

    /**
     * 处理恢复提醒
     *
     * @param taskId 任务ID
     * @param idempotentKey 幂等键
     */
    void handleRecover(Long taskId, String idempotentKey);

    /**
     * 处理免提醒回调
     *
     * @param idempotentKey 幂等键
     * @param callback 回调参数
     */
    void handleCallback(String idempotentKey, Map<String, Object> callback);

    /**
     * 升级通知
     *
     * @param taskId 任务ID
     */
    void escalate(Long taskId);
}
