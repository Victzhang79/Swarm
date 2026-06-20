package com.ruoyi.alarm.engine.service;

/**
 * 幂等收敛服务
 *
 * @author ruoyi
 */
public interface IdempotentService
{
    /**
     * 尝试获取幂等窗口
     *
     * @param idempotentKey     幂等键
     * @param minNotifyInterval 最小通知间隔（毫秒）
     * @return true=获取成功，false=窗口内重复
     */
    boolean tryAcquire(String idempotentKey, long minNotifyInterval);

    /**
     * 释放幂等窗口
     *
     * @param idempotentKey 幂等键
     */
    void release(String idempotentKey);

    /**
     * 查询是否处于免提醒状态
     *
     * @param idempotentKey 幂等键
     * @return true=免提醒期内，false=可提醒
     */
    boolean isIgnored(String idempotentKey);

    /**
     * 标记为免提醒状态
     *
     * @param idempotentKey 幂等键
     * @param ignoreInterval 忽略间隔（毫秒）
     */
    void markIgnored(String idempotentKey, long ignoreInterval);
}
