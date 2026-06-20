package com.ruoyi.alarm.domain;

import java.io.Serializable;
import com.ruoyi.common.annotation.Excel;
import com.ruoyi.common.core.domain.BaseEntity;

/**
 * 通知用户 Entity
 *
 * @author ruoyi
 */
public class AlarmNotifyUser extends BaseEntity implements Serializable
{
    private static final long serialVersionUID = 1L;

    /** 用户ID */
    @Excel(name = "用户ID")
    private Long userId;

    /** 用户名称 */
    @Excel(name = "用户名称")
    private String userName;

    /** Slack ID（敏感字段，需脱敏） */
    @Excel(name = "Slack ID")
    private String slackId;

    /** 企业微信ID */
    @Excel(name = "企业微信ID")
    private String wechatWorkId;

    /** 邮箱 */
    @Excel(name = "邮箱")
    private String email;

    /** 手机号（敏感字段，需脱敏） */
    @Excel(name = "手机号")
    private String phone;

    /** 内部推送ID */
    @Excel(name = "内部推送ID")
    private String innerPushId;

    /** VoIP ID（敏感字段，需脱敏） */
    @Excel(name = "VoIP ID")
    private String voipId;

    /** 状态（0=启用，1=禁用） */
    @Excel(name = "状态", readConverterExp = "0=启用,1=禁用")
    private String status;

    public Long getUserId()
    {
        return userId;
    }

    public void setUserId(Long userId)
    {
        this.userId = userId;
    }

    public String getUserName()
    {
        return userName;
    }

    public void setUserName(String userName)
    {
        this.userName = userName;
    }

    public String getSlackId()
    {
        return slackId;
    }

    public void setSlackId(String slackId)
    {
        this.slackId = slackId;
    }

    public String getWechatWorkId()
    {
        return wechatWorkId;
    }

    public void setWechatWorkId(String wechatWorkId)
    {
        this.wechatWorkId = wechatWorkId;
    }

    public String getEmail()
    {
        return email;
    }

    public void setEmail(String email)
    {
        this.email = email;
    }

    public String getPhone()
    {
        return phone;
    }

    public void setPhone(String phone)
    {
        this.phone = phone;
    }

    public String getInnerPushId()
    {
        return innerPushId;
    }

    public void setInnerPushId(String innerPushId)
    {
        this.innerPushId = innerPushId;
    }

    public String getVoipId()
    {
        return voipId;
    }

    public void setVoipId(String voipId)
    {
        this.voipId = voipId;
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
