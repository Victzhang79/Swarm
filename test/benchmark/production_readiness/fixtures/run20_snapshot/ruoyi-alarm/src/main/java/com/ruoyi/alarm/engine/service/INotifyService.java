package com.ruoyi.alarm.engine.service;

import java.util.Map;

/**
 * 通知渠道策略接口
 *
 * @author ruoyi
 */
public interface INotifyService
{
    /**
     * 获取渠道类型
     *
     * @return 渠道类型
     */
    String getChannelType();

    /**
     * 发送预警通知
     *
     * @param context 通知上下文
     */
    void sendNotify(Map<String, Object> context);

    /**
     * 发送恢复通知
     *
     * @param context 通知上下文
     */
    void sendRecover(Map<String, Object> context);

    /**
     * 处理回调
     *
     * @param callback 回调参数
     */
    void onCallback(Map<String, Object> callback);
}
