package com.ruoyi.alarm.domain.dto;

import java.util.Map;

/**
 * 预警任务测试DTO（用于测试发送）
 * 
 * @author ruoyi
 */
public class AlarmTaskTestDTO
{
    /** 任务ID */
    private Long taskId;

    /** 任务名称 */
    private String taskName;

    /** 应用ID */
    private Long appId;

    /** 应用密钥 */
    private String appSecret;

    /** 幂等值 */
    private String idempotentValue;

    /** 通知用户ID列表(逗号分隔) */
    private String notifyUserIds;

    /** 模板变量 */
    private Map<String, String> variables;

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

    public Long getAppId()
    {
        return appId;
    }

    public void setAppId(Long appId)
    {
        this.appId = appId;
    }

    public String getAppSecret()
    {
        return appSecret;
    }

    public void setAppSecret(String appSecret)
    {
        this.appSecret = appSecret;
    }

    public String getIdempotentValue()
    {
        return idempotentValue;
    }

    public void setIdempotentValue(String idempotentValue)
    {
        this.idempotentValue = idempotentValue;
    }

    public String getNotifyUserIds()
    {
        return notifyUserIds;
    }

    public void setNotifyUserIds(String notifyUserIds)
    {
        this.notifyUserIds = notifyUserIds;
    }

    public Map<String, String> getVariables()
    {
        return variables;
    }

    public void setVariables(Map<String, String> variables)
    {
        this.variables = variables;
    }
}
