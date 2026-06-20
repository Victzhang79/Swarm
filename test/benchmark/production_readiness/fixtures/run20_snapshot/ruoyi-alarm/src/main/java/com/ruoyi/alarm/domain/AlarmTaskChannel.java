package com.ruoyi.alarm.domain;

import java.util.Date;
import com.fasterxml.jackson.annotation.JsonFormat;
import com.ruoyi.common.annotation.Excel;
import com.ruoyi.common.core.domain.BaseEntity;

/**
 * 预警任务渠道配置实体 alarm_task_channel
 * 
 * @author ruoyi
 */
public class AlarmTaskChannel extends BaseEntity
{
    private static final long serialVersionUID = 1L;

    /** 主键ID */
    private Long id;

    /** 任务ID */
    private Long taskId;

    /** 渠道类型 slack/wechat_work/lark/voice/inner_push/voip */
    @Excel(name = "渠道类型")
    private String channelType;

    /** 机器人ID */
    private Long robotId;

    /** 机器人名称(显示用) */
    @Excel(name = "机器人名称")
    private String robotName;

    /** 模板ID */
    private Long templateId;

    /** 模板名称(显示用) */
    @Excel(name = "模板名称")
    private String templateName;

    /** 回调启用 0=否 1=是 */
    @Excel(name = "回调启用", readConverterExp = "0=否,1=是")
    private String callbackEnabled;

    /** 分隔符启用 0=否 1=是 */
    @Excel(name = "分隔符启用", readConverterExp = "0=否,1=是")
    private String dividerEnabled;

    /** 电话号码 */
    private String phoneNumber;

    /** 创建时间 */
    @JsonFormat(pattern = "yyyy-MM-dd HH:mm:ss", timezone = "GMT+8")
    @Excel(name = "创建时间", width = 30, dateFormat = "yyyy-MM-dd HH:mm:ss")
    private Date createTime;

    /** 更新时间 */
    @JsonFormat(pattern = "yyyy-MM-dd HH:mm:ss", timezone = "GMT+8")
    @Excel(name = "更新时间", width = 30, dateFormat = "yyyy-MM-dd HH:mm:ss")
    private Date updateTime;

    public Long getId()
    {
        return id;
    }

    public void setId(Long id)
    {
        this.id = id;
    }

    public Long getTaskId()
    {
        return taskId;
    }

    public void setTaskId(Long taskId)
    {
        this.taskId = taskId;
    }

    public String getChannelType()
    {
        return channelType;
    }

    public void setChannelType(String channelType)
    {
        this.channelType = channelType;
    }

    public Long getRobotId()
    {
        return robotId;
    }

    public void setRobotId(Long robotId)
    {
        this.robotId = robotId;
    }

    public String getRobotName()
    {
        return robotName;
    }

    public void setRobotName(String robotName)
    {
        this.robotName = robotName;
    }

    public Long getTemplateId()
    {
        return templateId;
    }

    public void setTemplateId(Long templateId)
    {
        this.templateId = templateId;
    }

    public String getTemplateName()
    {
        return templateName;
    }

    public void setTemplateName(String templateName)
    {
        this.templateName = templateName;
    }

    public String getCallbackEnabled()
    {
        return callbackEnabled;
    }

    public void setCallbackEnabled(String callbackEnabled)
    {
        this.callbackEnabled = callbackEnabled;
    }

    public String getDividerEnabled()
    {
        return dividerEnabled;
    }

    public void setDividerEnabled(String dividerEnabled)
    {
        this.dividerEnabled = dividerEnabled;
    }

    public String getPhoneNumber()
    {
        return phoneNumber;
    }

    public void setPhoneNumber(String phoneNumber)
    {
        this.phoneNumber = phoneNumber;
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
}
