package com.ruoyi.alarm.service;

import java.util.Date;
import java.util.List;
import com.ruoyi.alarm.domain.AlarmScheduleSnapshot;

/**
 * 排班快照 服务层
 * 
 * @author ruoyi
 */
public interface IAlarmScheduleSnapshotService
{
    /**
     * 查询排班快照列表
     * 
     * @param snapshot 排班快照对象
     * @return 排班快照集合
     */
    public List<AlarmScheduleSnapshot> selectSnapshotList(AlarmScheduleSnapshot snapshot);

    /**
     * 查询排班快照
     * 
     * @param snapshotId 快照ID
     * @return 排班快照
     */
    public AlarmScheduleSnapshot selectSnapshotById(Long snapshotId);

    /**
     * 生成排班快照
     * 
     * @param strategyId 策略ID
     * @param dutyDate 值班日期
     * @return 结果
     */
    public int generateSnapshot(Long strategyId, Date dutyDate);

    /**
     * 人工覆盖排班
     * 
     * @param snapshotId 快照ID
     * @param groupId 分组ID
     * @param memberIds 值班人ID列表
     * @return 结果
     */
    public int manualOverride(Long snapshotId, Long groupId, String memberIds);

    /**
     * 发送值班通知
     * 
     * @param snapshotId 快照ID
     * @return 结果
     */
    public int sendNotification(Long snapshotId);
}
