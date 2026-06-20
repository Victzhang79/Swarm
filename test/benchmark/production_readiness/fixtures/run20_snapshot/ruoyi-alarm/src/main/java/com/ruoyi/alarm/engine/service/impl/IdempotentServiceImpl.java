package com.ruoyi.alarm.engine.service.impl;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import org.springframework.stereotype.Service;
import com.ruoyi.alarm.engine.service.IdempotentService;

/**
 * 幂等收敛服务实现
 *
 * @author ruoyi
 */
@Service
public class IdempotentServiceImpl implements IdempotentService
{
    private static final String IDEMPOTENT_KEY_PREFIX = "alarm:idempotent:";
    private static final String IGNORE_KEY_PREFIX = "alarm:ignore:";

    /**
     * 内存缓存，用于模拟 Redis 缓存
     */
    private final Map<String, CacheEntry> cache = new ConcurrentHashMap<>();

    /**
     * 缓存条目
     */
    private static class CacheEntry
    {
        private final long expireTime;

        public CacheEntry(long expireTime)
        {
            this.expireTime = expireTime;
        }

        public boolean isExpired()
        {
            return System.currentTimeMillis() > expireTime;
        }
    }

    /**
     * 尝试获取幂等锁
     *
     * @param idempotentKey 幂等键
     * @param minNotifyInterval 最小通知间隔（毫秒）
     * @return true表示获取成功，false表示在窗口内被拒绝
     */
    @Override
    public boolean tryAcquire(String idempotentKey, long minNotifyInterval)
    {
        String key = IDEMPOTENT_KEY_PREFIX + idempotentKey;
        long expireTime = System.currentTimeMillis() + minNotifyInterval;
        
        // 模拟 Redis SET NX PX 操作
        CacheEntry existing = cache.get(key);
        if (existing != null && !existing.isExpired())
        {
            return false;
        }
        
        cache.put(key, new CacheEntry(expireTime));
        return true;
    }

    /**
     * 释放幂等锁
     *
     * @param idempotentKey 幂等键
     */
    @Override
    public void release(String idempotentKey)
    {
        String key = IDEMPOTENT_KEY_PREFIX + idempotentKey;
        cache.remove(key);
    }

    /**
     * 查询是否处于免提醒状态
     *
     * @param idempotentKey 幂等键
     * @return true表示处于免提醒状态
     */
    @Override
    public boolean isIgnored(String idempotentKey)
    {
        String key = IGNORE_KEY_PREFIX + idempotentKey;
        CacheEntry entry = cache.get(key);
        return entry != null && !entry.isExpired();
    }

    /**
     * 标记为免提醒状态
     *
     * @param idempotentKey 幂等键
     * @param ignoreInterval 免提醒间隔（毫秒）
     */
    @Override
    public void markIgnored(String idempotentKey, long ignoreInterval)
    {
        String key = IGNORE_KEY_PREFIX + idempotentKey;
        long expireTime = System.currentTimeMillis() + ignoreInterval;
        cache.put(key, new CacheEntry(expireTime));
    }
}
