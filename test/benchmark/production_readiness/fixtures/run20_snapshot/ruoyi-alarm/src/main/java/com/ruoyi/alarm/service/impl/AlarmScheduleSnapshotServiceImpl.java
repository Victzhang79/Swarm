package com.ruoyi.alarm.service.impl;

import java.util.Date;
import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import com.ruoyi.alarm.domain.AlarmHolidayPlan;
import com.ruoyi.alarm.domain.AlarmScheduleGroup;
import com.ruoyi.alarm.domain.AlarmScheduleSnapshot;
import com.ruoyi.alarm.domain.AlarmScheduleStrategy;
import com.ruoyi.alarm.mapper.AlarmHolidayPlanMapper;
import com.ruoyi.alarm.mapper.AlarmScheduleGroupMapper;
import com.ruoyi.alarm.mapper.AlarmScheduleSnapshotMapper;
import com.ruoyi.alarm.mapper.AlarmScheduleStrategyMapper;
import com.ruoyi.alarm.service.IAlarmScheduleSnapshotService;
import com.ruoyi.common.utils.DateUtils;

/**
 * 排班快照 Service业务层处理
 * 
 * @author ruoyi
 */
@Service
public class AlarmScheduleSnapshotServiceImpl implements IAlarmScheduleSnapshotService
{
    @Autowired
    private AlarmScheduleSnapshotMapper snapshotMapper;

    @Autowired
    private AlarmScheduleStrategyMapper strategyMapper;

    @Autowired
    private AlarmScheduleGroupMapper groupMapper;

    @Autowired
    private AlarmHolidayPlanMapper holidayPlanMapper;

    /**
     * 查询排班快照列表
     * 
     * @param snapshot 排班快照对象
     * @return 排班快照集合
     */
    @Override
    public List<AlarmScheduleSnapshot> selectSnapshotList(AlarmScheduleSnapshot snapshot)
    {
        return snapshotMapper.selectSnapshotList(snapshot);
    }

    /**
     * 查询排班快照
     * 
     * @param snapshotId 快照ID
     * @return 排班快照
     */
    @Override
    public AlarmScheduleSnapshot selectSnapshotById(Long snapshotId)
    {
        return snapshotMapper.selectSnapshotById(snapshotId);
    }

    /**
     * 生成排班快照
     * 
     * @param strategyId 策略ID
     * @param dutyDate 值班日期
     * @return 结果
     */
    @Override
    @Transactional
    public int generateSnapshot(Long strategyId, Date dutyDate)
    {
        // 查询排班策略
        AlarmScheduleStrategy strategy = strategyMapper.selectScheduleStrategyById(strategyId);
        if (strategy == null)
        {
            return 0;
        }

        // 查询排班分组列表
        List<AlarmScheduleGroup> groups = groupMapper.selectScheduleGroupByStrategyId(strategyId);
        if (groups == null || groups.isEmpty())
        {
            return 0;
        }

        // 创建快照对象
        AlarmScheduleSnapshot snapshot = new AlarmScheduleSnapshot();
        snapshot.setStrategyId(strategyId);
        snapshot.setDutyDate(dutyDate);
        snapshot.setIsManualOverride("0");
        snapshot.setNotifyStatus("pending");

        // 根据策略模式生成快照
        String strategyMode = strategy.getStrategyMode();
        if ("holiday_priority".equals(strategyMode))
        {
            // 节假日优先模式：先查节假日计划
            AlarmHolidayPlan holidayPlan = holidayPlanMapper.selectHolidayByDate(dutyDate, strategyId);
            if (holidayPlan != null)
            {
                snapshot.setScheduleSource("holiday");
                snapshot.setMemberIds(holidayPlan.getMemberIds());
            }
            else
            {
                // 非节假日，走轮询模式
                generateRotationSnapshot(snapshot, groups, dutyDate);
            }
        }
        else
        {
            // 默认轮询模式
            generateRotationSnapshot(snapshot, groups, dutyDate);
        }

        return snapshotMapper.insertSnapshot(snapshot);
    }

    /**
     * 轮询模式生成快照
     */
    private void generateRotationSnapshot(AlarmScheduleSnapshot snapshot, List<AlarmScheduleGroup> groups, Date dutyDate)
    {
        snapshot.setScheduleSource("rotation");

        // 查询上一日快照获取当前group_index
        Date yesterday = DateUtils.addDays(dutyDate, -1);
        AlarmScheduleSnapshot yesterdaySnapshot = snapshotMapper.selectSnapshotByDateAndStrategy(yesterday, snapshot.getStrategyId());

        int currentIndex = 0;
        if (yesterdaySnapshot != null && yesterdaySnapshot.getGroupId() != null)
        {
            // 找到上一日分组在列表中的索引
            for (int i = 0; i < groups.size(); i++)
            {
                if (groups.get(i).getGroupId().equals(yesterdaySnapshot.getGroupId()))
                {
                    currentIndex = i;
                    break;
                }
            }
        }

        // 取下一个分组（循环）
        int nextIndex = (currentIndex + 1) % groups.size();
        AlarmScheduleGroup nextGroup = groups.get(nextIndex);

        snapshot.setGroupId(nextGroup.getGroupId());
        snapshot.setMemberIds(nextGroup.getMemberIds());
    }

    /**
     * 人工覆盖排班
     * 
     * @param snapshotId 快照ID
     * @param groupId 分组ID
     * @param memberIds 值班人ID列表
     * @return 结果
     */
    @Override
    @Transactional
    public int manualOverride(Long snapshotId, Long groupId, String memberIds)
    {
        return snapshotMapper.updateManualOverride(snapshotId, groupId, memberIds);
    }

    /**
     * 发送值班通知
     * 
     * @param snapshotId 快照ID
     * @return 结果
     */
    @Override
    @Transactional
    public int sendNotification(Long snapshotId)
    {
        AlarmScheduleSnapshot snapshot = snapshotMapper.selectSnapshotById(snapshotId);
        if (snapshot == null)
        {
            return 0;
        }

        // TODO: 调用INotifyService发送Slack DM值班通知
        // 跨模块依赖，由alarm-core提供
        // notifyService.sendSlackDmNotification(snapshot.getMemberIds(), snapshot.getDutyDate());

        // 更新通知状态为已发送
        return snapshotMapper.updateNotifyStatus(snapshotId, "sent");
    }
}
