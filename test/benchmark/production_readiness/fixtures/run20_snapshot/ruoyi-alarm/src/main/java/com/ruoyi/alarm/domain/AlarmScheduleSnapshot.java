package com.ruoyi.alarm.domain;

import java.io.Serializable;
import java.util.Date;
import com.fasterxml.jackson.annotation.JsonFormat;
import com.ruoyi.common.core.domain.BaseEntity;

/**
 * 排班快照 alarm_schedule_snapshot
 * 
 * @author ruoyi
 */
public class AlarmScheduleSnapshot extends BaseEntity implements Serializable
{
    private static final long serialVersionUID = 1L;

    /** 快照ID */
    private Long snapshotId;

    /** 任务ID */
    private Long taskId;

    /** 策略ID */
    private Long strategyId;

    /** 值班日期 */
    @JsonFormat(pattern = "yyyy-MM-dd", timezone = "GMT+8", shape = com.fasterxml.jackson.annotation.JsonFormat.Shape.STRING)
    private Date dutyDate;

    /** 排班来源(rotation|holiday|manual) */
    private String scheduleSource;

    /** 分组ID */
    private Long groupId;

    /** 值班人ID列表(逗号分隔) */
    private String memberIds;

    /** 是否人工覆盖(0否1是) */
    private String isManualOverride;

    /** 通知状态(pending|sent|failed) */
    private String notifyStatus;

    public Long getSnapshotId()
    {
        return snapshotId;
    }

    public void setSnapshotId(Long snapshotId)
    {
        this.snapshotId = snapshotId;
    }

    public Long getTaskId()
    {
        return taskId;
    }

    public void setTaskId(Long taskId)
    {
        this.taskId = taskId;
    }

    public Long getStrategyId()
    {
        return strategyId;
    }

    public void setStrategyId(Long strategyId)
    {
        this.strategyId = strategyId;
    }

    public Date getDutyDate()
    {
        return dutyDate;
    }

    public void setDutyDate(Date dutyDate)
    {
        this.dutyDate = dutyDate;
    }

    public String getScheduleSource()
    {
        return scheduleSource;
    }

    public void setScheduleSource(String scheduleSource)
    {
        this.scheduleSource = scheduleSource;
    }

    public Long getGroupId()
    {
        return groupId;
    }

    public void setGroupId(Long groupId)
    {
        this.groupId = groupId;
    }

    public String getMemberIds()
    {
        return memberIds;
    }

    public void setMemberIds(String memberIds)
    {
        this.memberIds = memberIds;
    }

    public String getIsManualOverride()
    {
        return isManualOverride;
    }

    public void setIsManualOverride(String isManualOverride)
    {
        this.isManualOverride = isManualOverride;
    }

    public String getNotifyStatus()
    {
        return notifyStatus;
    }

    public void setNotifyStatus(String notifyStatus)
    {
        this.notifyStatus = notifyStatus;
    }
}
