package com.ruoyi.alarm.engine.service.impl;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import com.ruoyi.alarm.engine.service.AlarmEngineService;
import com.ruoyi.alarm.engine.service.IdempotentService;
import com.ruoyi.alarm.engine.service.INotifyService;
import com.ruoyi.alarm.engine.service.TemplateRenderService;
import com.ruoyi.common.utils.StringUtils;

/**
 * 预警引擎调度服务实现
 *
 * @author ruoyi
 */
@Service
public class AlarmEngineServiceImpl implements AlarmEngineService
{
    private static final Logger log = LoggerFactory.getLogger(AlarmEngineServiceImpl.class);

    /**
     * 语音渠道类型
     */
    private static final String CHANNEL_VOICE = "voice";

    /**
     * VoIP渠道类型
     */
    private static final String CHANNEL_VOIP = "voip";

    /**
     * 异步线程池
     */
    private static final ExecutorService asyncExecutor = Executors.newFixedThreadPool(
            4,
            new ThreadFactory()
            {
                private int count = 0;
                @Override
                public Thread newThread(Runnable r)
                {
                    Thread thread = new Thread(r, "alarm-engine-async-" + count++);
                    thread.setDaemon(true);
                    return thread;
                }
            }
    );

    @Autowired
    private Map<String, INotifyService> notifyServiceMap;

    @Autowired
    private IdempotentService idempotentService;

    @Autowired
    private TemplateRenderService templateRenderService;

    /**
     * 升级轮询索引，记录每个任务的当前轮询位置
     */
    private final Map<Long, Integer> escalateIndexMap = new ConcurrentHashMap<>();

    /**
     * 异步分发预警通知
     *
     * @param taskConfig 任务配置
     * @param variables 变量映射
     */
    @Override
    public void dispatch(Map<String, Object> taskConfig, Map<String, String> variables)
    {
        final Map<String, Object> finalTaskConfig = taskConfig;
        final Map<String, String> finalVariables = variables;
        asyncExecutor.execute(new Runnable()
        {
            @Override
            public void run()
            {
                try
                {
                    doDispatch(finalTaskConfig, finalVariables);
                }
                catch (Exception e)
                {
                    log.error("AlarmEngine dispatch error", e);
                }
            }
        });
    }

    /**
     * 执行分发逻辑
     */
    private void doDispatch(Map<String, Object> taskConfig, Map<String, String> variables)
    {
        // 1. 幂等收敛判断
        String idempotentKey = castToString(taskConfig.get("idempotentKey"));
        Long minNotifyInterval = castToLong(taskConfig.get("minNotifyInterval"));

        if (StringUtils.isEmpty(idempotentKey) || minNotifyInterval == null)
        {
            log.warn("AlarmEngine dispatch failed, idempotentKey or minNotifyInterval is empty");
            return;
        }

        if (!idempotentService.tryAcquire(idempotentKey, minNotifyInterval))
        {
            log.info("AlarmEngine dispatch skipped due to idempotent, idempotentKey: {}", idempotentKey);
            return;
        }

        // 2. 模板渲染
        String templateContent = castToString(taskConfig.get("templateContent"));
        String content = templateRenderService.render(templateContent, variables);

        // 3. 按 task_channel 配置遍历，调用对应渠道
        List<Map<String, Object>> taskChannels = castToList(taskConfig.get("taskChannels"));
        if (taskChannels == null || taskChannels.isEmpty())
        {
            log.warn("AlarmEngine dispatch failed, taskChannels is empty");
            return;
        }

        boolean isRecover = castToBoolean(taskConfig.get("isRecover"));

        for (Map<String, Object> channel : taskChannels)
        {
            String channelType = castToString(channel.get("channelType"));

            // voice/voip 渠道在恢复场景跳过
            if (isRecover && (CHANNEL_VOICE.equals(channelType) || CHANNEL_VOIP.equals(channelType)))
            {
                log.info("AlarmEngine dispatch skip {} channel for recover scenario", channelType);
                continue;
            }

            INotifyService notifyService = notifyServiceMap.get(channelType);
            if (notifyService == null)
            {
                log.warn("AlarmEngine dispatch failed, no notify service for channel: {}", channelType);
                continue;
            }

            Map<String, Object> context = new ConcurrentHashMap<>();
            context.putAll(taskConfig);
            context.putAll(channel);
            context.put("content", content);
            context.put("idempotentKey", idempotentKey);

            notifyService.sendNotify(context);
            log.info("AlarmEngine dispatch success for channel: {}", channelType);
        }
    }

    /**
     * 处理恢复提醒
     *
     * @param taskId 任务ID
     * @param idempotentKey 幂等键
     */
    @Override
    public void handleRecover(final Long taskId, final String idempotentKey)
    {
        asyncExecutor.execute(new Runnable()
        {
            @Override
            public void run()
            {
                try
                {
                    // 1. 释放幂等窗口
                    idempotentService.release(idempotentKey);

                    // 2. 遍历渠道调用 sendRecover（跳过 voice/voip）
                    for (Map.Entry<String, INotifyService> entry : notifyServiceMap.entrySet())
                    {
                        String channelType = entry.getKey();
                        if (CHANNEL_VOICE.equals(channelType) || CHANNEL_VOIP.equals(channelType))
                        {
                            log.info("AlarmEngine handleRecover skip {} channel", channelType);
                            continue;
                        }

                        INotifyService notifyService = entry.getValue();
                        Map<String, Object> context = new ConcurrentHashMap<>();
                        context.put("taskId", taskId);
                        context.put("idempotentKey", idempotentKey);
                        context.put("isRecover", true);

                        notifyService.sendRecover(context);
                        log.info("AlarmEngine handleRecover success for channel: {}", channelType);
                    }
                }
                catch (Exception e)
                {
                    log.error("AlarmEngine handleRecover error", e);
                }
            }
        });
    }

    /**
     * 处理免提醒回调
     *
     * @param idempotentKey 幂等键
     * @param callback 回调参数
     */
    @Override
    public void handleCallback(final String idempotentKey, final Map<String, Object> callback)
    {
        asyncExecutor.execute(new Runnable()
        {
            @Override
            public void run()
            {
                try
                {
                    // 1. 调用对应渠道 onCallback
                    String channelType = castToString(callback.get("channelType"));
                    if (StringUtils.isNotEmpty(channelType))
                    {
                        INotifyService notifyService = notifyServiceMap.get(channelType);
                        if (notifyService != null)
                        {
                            notifyService.onCallback(callback);
                        }
                    }

                    // 2. 标记为免提醒状态
                    Long ignoreInterval = castToLong(callback.get("ignoreInterval"));
                    if (ignoreInterval != null)
                    {
                        idempotentService.markIgnored(idempotentKey, ignoreInterval);
                        log.info("AlarmEngine handleCallback marked ignored for idempotentKey: {}", idempotentKey);
                    }
                }
                catch (Exception e)
                {
                    log.error("AlarmEngine handleCallback error", e);
                }
            }
        });
    }

    /**
     * 升级通知
     *
     * @param taskId 任务ID
     */
    @Override
    public void escalate(final Long taskId)
    {
        asyncExecutor.execute(new Runnable()
        {
            @Override
            public void run()
            {
                try
                {
                    // 按 schedule_strategy 轮询切换下一组排班成员
                    int currentIndex = escalateIndexMap.getOrDefault(taskId, 0);
                    int nextIndex = (currentIndex + 1) % 3; // 假设3组排班
                    escalateIndexMap.put(taskId, nextIndex);

                    log.info("AlarmEngine escalate for taskId: {}, current index: {}, next index: {}",
                            taskId, currentIndex, nextIndex);

                    // 重新触发 dispatch
                    Map<String, Object> taskConfig = new ConcurrentHashMap<>();
                    taskConfig.put("taskId", taskId);
                    taskConfig.put("escalateIndex", nextIndex);
                    // 重新分发
                    dispatch(taskConfig, new ConcurrentHashMap<>());
                }
                catch (Exception e)
                {
                    log.error("AlarmEngine escalate error", e);
                }
            }
        });
    }

    private String castToString(Object obj)
    {
        return obj != null ? obj.toString() : null;
    }

    private Long castToLong(Object obj)
    {
        if (obj == null)
        {
            return null;
        }
        try
        {
            return Long.parseLong(obj.toString());
        }
        catch (NumberFormatException e)
        {
            return null;
        }
    }

    private boolean castToBoolean(Object obj)
    {
        if (obj instanceof Boolean)
        {
            return (Boolean) obj;
        }
        if (obj instanceof String)
        {
            return Boolean.parseBoolean((String) obj);
        }
        return false;
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> castToList(Object obj)
    {
        if (obj instanceof List)
        {
            return (List<Map<String, Object>>) obj;
        }
        return null;
    }
}
