package com.ruoyi.alarm.domain;

import java.io.Serializable;
import com.ruoyi.common.core.domain.BaseEntity;

/**
 * 机器人 alarm_robot
 *
 * @author ruoyi
 */
public class AlarmRobot extends BaseEntity implements Serializable
{
    private static final long serialVersionUID = 1L;

    /** 机器人ID */
    private Long robotId;

    /** 机器人类型(slack_bot/wechat_work_bot/lark_bot) */
    private String robotType;

    /** 机器人名称 */
    private String robotName;

    /** Webhook地址 */
    private String webhookUrl;

    /** 状态(0=启用,1=禁用) */
    private String status;

    public Long getRobotId()
    {
        return robotId;
    }

    public void setRobotId(Long robotId)
    {
        this.robotId = robotId;
    }

    public String getRobotType()
    {
        return robotType;
    }

    public void setRobotType(String robotType)
    {
        this.robotType = robotType;
    }

    public String getRobotName()
    {
        return robotName;
    }

    public void setRobotName(String robotName)
    {
        this.robotName = robotName;
    }

    public String getWebhookUrl()
    {
        return webhookUrl;
    }

    public void setWebhookUrl(String webhookUrl)
    {
        this.webhookUrl = webhookUrl;
    }

    public String getStatus()
    {
        return status;
    }

    public void setStatus(String status)
    {
        this.status = status;
    }
}
