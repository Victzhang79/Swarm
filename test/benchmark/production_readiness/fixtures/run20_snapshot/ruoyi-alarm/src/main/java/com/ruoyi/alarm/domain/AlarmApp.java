package com.ruoyi.alarm.domain;

import java.util.Date;
import com.fasterxml.jackson.annotation.JsonFormat;
import com.ruoyi.common.core.domain.BaseEntity;

/**
 * 预警应用 Entity
 *
 * @author ruoyi
 */
public class AlarmApp extends BaseEntity
{
    private static final long serialVersionUID = 1L;

    /** 应用ID */
    private Long appId;

    /** 应用名称 */
    private String appName;

    /** 应用秘钥 */
    private String appSecret;

    /** 状态（0=启用，1=禁用） */
    private String status;

    /** 过期时间 */
    @JsonFormat(pattern = "yyyy-MM-dd HH:mm:ss", timezone = "GMT+8")
    private Date expireTime;

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

    public String getAppSecret()
    {
        return appSecret;
    }

    public void setAppSecret(String appSecret)
    {
        this.appSecret = appSecret;
    }

    public String getStatus()
    {
        return status;
    }

    public void setStatus(String status)
    {
        this.status = status;
    }

    public Date getExpireTime()
    {
        return expireTime;
    }

    public void setExpireTime(Date expireTime)
    {
        this.expireTime = expireTime;
    }

    @Override
    public String toString()
    {
        return "AlarmApp{" +
            "appId=" + appId +
            ", appName='" + appName + "'" +
            ", appSecret='" + appSecret + "'" +
            ", status='" + status + "'" +
            ", expireTime=" + expireTime +
            ", createBy='" + getCreateBy() + "'" +
            ", createTime=" + getCreateTime() +
            ", updateBy='" + getUpdateBy() + "'" +
            ", updateTime=" + getUpdateTime() +
            ", remark='" + getRemark() + "'" +
            "}";
    }
}
