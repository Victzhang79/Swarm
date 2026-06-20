package com.ruoyi.alarm.task;

import java.util.Date;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;
import com.ruoyi.alarm.domain.AlarmScheduleSnapshot;
import com.ruoyi.alarm.domain.AlarmScheduleStrategy;
import com.ruoyi.alarm.mapper.AlarmScheduleSnapshotMapper;
import com.ruoyi.alarm.mapper.AlarmScheduleStrategyMapper;
import com.ruoyi.alarm.service.IAlarmScheduleSnapshotService;
import com.ruoyi.common.utils.DateUtils;

/**
 * 排班快照定时任务
 * 每日18:00自动查询所有启用状态的排班策略，为每个策略生成次日排班快照并异步发送值班通知
 * 
 * @author ruoyi
 */
@Component("scheduleSnapshotTask")
public class ScheduleSnapshotTask
{
    private static final Logger log = LoggerFactory.getLogger(ScheduleSnapshotTask.class);

    @Autowired
    private AlarmScheduleStrategyMapper strategyMapper;

    @Autowired
    private IAlarmScheduleSnapshotService snapshotService;

    @Autowired
    private AlarmScheduleSnapshotMapper snapshotMapper;

    /**
     * 执行定时任务
     */
    public void execute()
    {
        log.info("开始执行排班快照定时任务");
        try
        {
            // 查询所有启用状态的排班策略
            AlarmScheduleStrategy query = new AlarmScheduleStrategy();
            query.setStatus("0");
            List<AlarmScheduleStrategy> strategies = strategyMapper.selectScheduleStrategyList(query);

            if (strategies == null || strategies.isEmpty())
            {
                log.info("未找到启用状态的排班策略");
                return;
            }

            // 计算次日日期
            Date tomorrow = DateUtils.addDays(new Date(), 1);

            int successCount = 0;
            int failCount = 0;

            for (AlarmScheduleStrategy strategy : strategies)
            {
                try
                {
                    // 检查是否已存在当日快照
                    AlarmScheduleSnapshot existingSnapshot = snapshotMapper.selectSnapshotByDateAndStrategy(tomorrow, strategy.getStrategyId());
                    if (existingSnapshot != null)
                    {
                        log.info("策略[{}]已存在次日快照，跳过", strategy.getStrategyName());
                        continue;
                    }

                    // 生成排班快照
                    int result = snapshotService.generateSnapshot(strategy.getStrategyId(), tomorrow);
                    if (result > 0)
                    {
                        successCount++;
                        log.info("成功生成策略[{}]的次日排班快照", strategy.getStrategyName());
                    }
                }
                catch (Exception e)
                {
                    failCount++;
                    log.error("生成策略[{}]的排班快照失败", strategy.getStrategyName(), e);
                }
            }

            // 异步发送值班通知
            sendNotifications(tomorrow);

            log.info("排班快照定时任务执行完成，成功：{}，失败：{}", successCount, failCount);
        }
        catch (Exception e)
        {
            log.error("排班快照定时任务执行异常", e);
        }
    }

    /**
     * 异步发送值班通知
     * 
     * @param dutyDate 值班日期
     */
    private void sendNotifications(Date dutyDate)
    {
        try
        {
            // 查询当日所有待发送通知的快照
            AlarmScheduleSnapshot query = new AlarmScheduleSnapshot();
            query.setDutyDate(dutyDate);
            query.setNotifyStatus("pending");
            List<AlarmScheduleSnapshot> snapshots = snapshotService.selectSnapshotList(query);

            for (AlarmScheduleSnapshot snapshot : snapshots)
            {
                try
                {
                    snapshotService.sendNotification(snapshot.getSnapshotId());
                    log.info("成功发送快照[{}]的值班通知", snapshot.getSnapshotId());
                }
                catch (Exception e)
                {
                    log.error("发送快照[{}]的值班通知失败", snapshot.getSnapshotId(), e);
                    // 更新通知状态为失败
                    snapshotMapper.updateNotifyStatus(snapshot.getSnapshotId(), "failed");
                }
            }
        }
        catch (Exception e)
        {
            log.error("发送值班通知异常", e);
        }
    }
}
