package com.ruoyi.alarm.engine.task;

import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;
import com.ruoyi.alarm.engine.service.AlarmEngineService;
import com.ruoyi.common.annotation.Log;
import com.ruoyi.common.enums.BusinessType;

/**
 * 预警升级定时任务
 *
 * @author ruoyi
 */
@Component("alarmEscalationTask")
public class AlarmEscalationTask
{
    private static final Logger log = LoggerFactory.getLogger(AlarmEscalationTask.class);

    @Autowired
    private AlarmEngineService alarmEngineService;

    /**
     * 预警升级任务
     * 查询未处理(未免提醒)的 P1/P2 预警任务列表，对每个任务调用 escalate 触发升级通知
     */
    @Log(title = "预警升级", businessType = BusinessType.OTHER)
    public void escalateAlarm()
    {
        log.info("Alarm escalation task started");
        try
        {
            // 查询未处理的 P1/P2 预警任务
            List<Map<String, Object>> unprocessedTasks = queryUnprocessedP1P2Tasks();
            log.info("Found {} unprocessed P1/P2 alarm tasks", unprocessedTasks.size());

            int successCount = 0;
            int failCount = 0;

            for (Map<String, Object> task : unprocessedTasks)
            {
                try
                {
                    Long taskId = Long.valueOf(task.get("taskId").toString());
                    alarmEngineService.escalate(taskId);
                    successCount++;
                    log.info("Alarm escalation success for taskId: {}", taskId);
                }
                catch (Exception e)
                {
                    failCount++;
                    log.error("Alarm escalation failed for task: {}", task, e);
                }
            }

            log.info("Alarm escalation task completed, total: {}, success: {}, fail: {}",
                    unprocessedTasks.size(), successCount, failCount);
        }
        catch (Exception e)
        {
            log.error("Alarm escalation task failed", e);
        }
    }

    /**
     * 查询未处理的 P1/P2 预警任务
     *
     * @return 未处理的预警任务列表
     */
    private List<Map<String, Object>> queryUnprocessedP1P2Tasks()
    {
        // TODO: 实现查询逻辑，查询未处理(未免提醒)的 P1/P2 预警任务
        // SELECT task_id, task_name, alarm_level, alarm_type, idempotent_value,
        //        min_notify_interval, ignore_interval, app_id, schedule_strategy_id,
        //        notify_user_ids
        // FROM alarm_task
        // WHERE status = '0'
        //   AND alarm_level IN ('P1', 'P2')
        //   AND ignore_end_time < NOW()
        return new java.util.ArrayList<>();
    }
}
