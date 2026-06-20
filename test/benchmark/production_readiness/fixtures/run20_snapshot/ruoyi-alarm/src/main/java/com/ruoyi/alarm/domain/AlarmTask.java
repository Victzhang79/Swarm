package com.ruoyi.alarm.domain;

import java.util.Date;
import java.util.List;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import com.fasterxml.jackson.annotation.JsonFormat;
import com.ruoyi.common.annotation.Excel;
import com.ruoyi.common.core.domain.BaseEntity;

/**
 * 预警任务实体 alarm_task
 * 
 * @author ruoyi
 */
public class AlarmTask extends BaseEntity
{
    private static final long serialVersionUID = 1L;

    /** 任务ID */
    private Long taskId;

    /** 任务名称 */
    @NotBlank(message = "任务名称不能为空")
    @Size(max = 100, message = "任务名称长度不能超过100")
    @Excel(name = "任务名称")
    private String taskName;

    /** 状态 0=启用 1=禁用 */
    @Excel(name = "状态", readConverterExp = "0=启用,1=禁用")
    private String status;

    /** 告警级别 P1=紧急 P2=需立即处理 P3=消息提醒 */
    @Excel(name = "告警级别", readConverterExp = "P1=紧急,P2=需立即处理,P3=消息提醒")
    private String alarmLevel;

    /** 告警类型 业务消息/服务异常/安全问题 */
    @Excel(name = "告警类型")
    private String alarmType;

    /** 幂等值 */
    private String idempotentValue;

    /** 最小通知间隔(秒) */
    @Excel(name = "最小通知间隔(秒)")
    private Long minNotifyInterval;

    /** 忽略间隔(秒) */
    @Excel(name = "忽略间隔(秒)")
    private Long ignoreInterval;

    /** 最小累计次数 */
    @Excel(name = "最小累计次数")
    private Integer minAccumulateCount;

    /** 应用ID */
    private Long appId;

    /** 应用名称(显示用) */
    @Excel(name = "应用名称")
    private String appName;

    /** 排班策略ID */
    private Long scheduleStrategyId;

    /** 排班策略名称(显示用) */
    @Excel(name = "排班策略名称")
    private String scheduleStrategyName;

    /** 通知用户ID列表(逗号分隔) */
    private String notifyUserIds;

    /** 通知用户列表(显示用) */
    @Excel(name = "通知用户")
    private String notifyUserNames;

    /** 创建时间 */
    @JsonFormat(pattern = "yyyy-MM-dd HH:mm:ss", timezone = "GMT+8")
    @Excel(name = "创建时间", width = 30, dateFormat = "yyyy-MM-dd HH:mm:ss")
    private Date createTime;

    /** 更新时间 */
    @JsonFormat(pattern = "yyyy-MM-dd HH:mm:ss", timezone = "GMT+8")
    @Excel(name = "更新时间", width = 30, dateFormat = "yyyy-MM-dd HH:mm:ss")
    private Date updateTime;

    /** 渠道配置列表 */
    private List<AlarmTaskChannel> channels;

    public Long getTaskId()
    {
        return taskId;
    }

    public void setTaskId(Long taskId)
    {
        this.taskId = taskId;
    }

    public String getTaskName()
    {
        return taskName;
    }

    public void setTaskName(String taskName)
    {
        this.taskName = taskName;
    }

    public String getStatus()
    {
        return status;
    }

    public void setStatus(String status)
    {
        this.status = status;
    }

    public String getAlarmLevel()
    {
        return alarmLevel;
    }

    public void setAlarmLevel(String alarmLevel)
    {
        this.alarmLevel = alarmLevel;
    }

    public String getAlarmType()
    {
        return alarmType;
    }

    public void setAlarmType(String alarmType)
    {
        this.alarmType = alarmType;
    }

    public String getIdempotentValue()
    {
        return idempotentValue;
    }

    public void setIdempotentValue(String idempotentValue)
    {
        this.idempotentValue = idempotentValue;
    }

    public Long getMinNotifyInterval()
    {
        return minNotifyInterval;
    }

    public void setMinNotifyInterval(Long minNotifyInterval)
    {
        this.minNotifyInterval = minNotifyInterval;
    }

    public Long getIgnoreInterval()
    {
        return ignoreInterval;
    }

    public void setIgnoreInterval(Long ignoreInterval)
    {
        this.ignoreInterval = ignoreInterval;
    }

    public Integer getMinAccumulateCount()
    {
        return minAccumulateCount;
    }

    public void setMinAccumulateCount(Integer minAccumulateCount)
    {
        this.minAccumulateCount = minAccumulateCount;
    }

    public Long getAppId()
    {
        return appId;
    }

    public void setAppId(Long appId)
    {
        this.appId = appId;
    }

    public String getAppName()
    {
        return appName;
    }

    public void setAppName(String appName)
    {
        this.appName = appName;
    }

    public Long getScheduleStrategyId()
    {
        return scheduleStrategyId;
    }

    public void setScheduleStrategyId(Long scheduleStrategyId)
    {
        this.scheduleStrategyId = scheduleStrategyId;
    }

    public String getScheduleStrategyName()
    {
        return scheduleStrategyName;
    }

    public void setScheduleStrategyName(String scheduleStrategyName)
    {
        this.scheduleStrategyName = scheduleStrategyName;
    }

    public String getNotifyUserIds()
    {
        return notifyUserIds;
    }

    public void setNotifyUserIds(String notifyUserIds)
    {
        this.notifyUserIds = notifyUserIds;
    }

    public String getNotifyUserNames()
    {
        return notifyUserNames;
    }

    public void setNotifyUserNames(String notifyUserNames)
    {
        this.notifyUserNames = notifyUserNames;
    }

    public Date getCreateTime()
    {
        return createTime;
    }

    public void setCreateTime(Date createTime)
    {
        this.createTime = createTime;
    }

    public Date getUpdateTime()
    {
        return updateTime;
    }

    public void setUpdateTime(Date updateTime)
    {
        this.updateTime = updateTime;
    }

    public List<AlarmTaskChannel> getChannels()
    {
        return channels;
    }

    public void setChannels(List<AlarmTaskChannel> channels)
    {
        this.channels = channels;
    }
}
