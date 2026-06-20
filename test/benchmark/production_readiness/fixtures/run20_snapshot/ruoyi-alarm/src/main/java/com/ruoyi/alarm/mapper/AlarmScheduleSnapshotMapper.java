package com.ruoyi.alarm.mapper;

import java.util.Date;
import java.util.List;
import org.apache.ibatis.annotations.Param;
import com.ruoyi.alarm.domain.AlarmScheduleSnapshot;

/**
 * 排班快照 数据层
 * 
 * @author ruoyi
 */
public interface AlarmScheduleSnapshotMapper
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
     * 根据值班日期和策略ID查询排班快照
     * 
     * @param dutyDate 值班日期
     * @param strategyId 策略ID
     * @return 排班快照
     */
    public AlarmScheduleSnapshot selectSnapshotByDateAndStrategy(@Param("dutyDate") Date dutyDate, @Param("strategyId") Long strategyId);

    /**
     * 新增排班快照
     * 
     * @param snapshot 排班快照对象
     * @return 结果
     */
    public int insertSnapshot(AlarmScheduleSnapshot snapshot);

    /**
     * 修改排班快照
     * 
     * @param snapshot 排班快照对象
     * @return 结果
     */
    public int updateSnapshot(AlarmScheduleSnapshot snapshot);

    /**
     * 更新人工覆盖
     * 
     * @param snapshotId 快照ID
     * @param groupId 分组ID
     * @param memberIds 值班人ID列表
     * @return 结果
     */
    public int updateManualOverride(@Param("snapshotId") Long snapshotId, @Param("groupId") Long groupId, @Param("memberIds") String memberIds);

    /**
     * 更新通知状态
     * 
     * @param snapshotId 快照ID
     * @param notifyStatus 通知状态
     * @return 结果
     */
    public int updateNotifyStatus(@Param("snapshotId") Long snapshotId, @Param("notifyStatus") String notifyStatus);

    /**
     * 批量删除排班快照
     * 
     * @param snapshotIds 需要删除的快照ID
     * @return 结果
     */
    public int deleteSnapshotByIds(Long[] snapshotIds);
}
