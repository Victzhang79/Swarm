package com.ruoyi.alarm.sdk;

import com.alibaba.fastjson.JSON;
import com.alibaba.fastjson.JSONObject;

import java.util.HashMap;
import java.util.Map;

/**
 * 预警消息实体类
 *
 * @author ruoyi
 */
public class AlarmMessage
{
    /** 任务名称 */
    private String taskName;

    /** 预警级别 P1/P2/P3 */
    private String alarmLevel;

    /** 预警类型 */
    private String alarmType;

    /** 幂等值 */
    private String idempotentValue;

    /** 模板变量 */
    private Map<String, String> templateVars;

    /** 扩展字段 */
    private Map<String, Object> extra;

    private AlarmMessage()
    {
    }

    /**
     * 获取 Builder
     *
     * @return builder 实例
     */
    public static Builder builder()
    {
        return new Builder();
    }

    /**
     * Builder 模式构建器
     */
    public static class Builder
    {
        private final AlarmMessage message;

        public Builder()
        {
            this.message = new AlarmMessage();
        }

        public Builder taskName(String taskName)
        {
            this.message.taskName = taskName;
            return this;
        }

        public Builder alarmLevel(String alarmLevel)
        {
            this.message.alarmLevel = alarmLevel;
            return this;
        }

        public Builder alarmType(String alarmType)
        {
            this.message.alarmType = alarmType;
            return this;
        }

        public Builder idempotentValue(String idempotentValue)
        {
            this.message.idempotentValue = idempotentValue;
            return this;
        }

        public Builder templateVars(Map<String, String> templateVars)
        {
            this.message.templateVars = templateVars;
            return this;
        }

        public Builder extra(Map<String, Object> extra)
        {
            this.message.extra = extra;
            return this;
        }

        public AlarmMessage build()
        {
            return this.message;
        }
    }

    /**
     * 序列化为 Map
     *
     * @return Map 对象
     */
    public Map<String, Object> toMap()
    {
        Map<String, Object> map = new HashMap<>();
        if (taskName != null)
        {
            map.put("taskName", taskName);
        }
        if (alarmLevel != null)
        {
            map.put("alarmLevel", alarmLevel);
        }
        if (alarmType != null)
        {
            map.put("alarmType", alarmType);
        }
        if (idempotentValue != null)
        {
            map.put("idempotentValue", idempotentValue);
        }
        if (templateVars != null && !templateVars.isEmpty())
        {
            map.put("templateVars", templateVars);
        }
        if (extra != null && !extra.isEmpty())
        {
            map.put("extra", extra);
        }
        return map;
    }

    /**
     * 序列化为 JSON 字符串
     *
     * @return JSON 字符串
     */
    public String toJson()
    {
        return JSON.toJSONString(this);
    }

    public String getTaskName()
    {
        return taskName;
    }

    public void setTaskName(String taskName)
    {
        this.taskName = taskName;
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

    public Map<String, String> getTemplateVars()
    {
        return templateVars;
    }

    public void setTemplateVars(Map<String, String> templateVars)
    {
        this.templateVars = templateVars;
    }

    public Map<String, Object> getExtra()
    {
        return extra;
    }

    public void setExtra(Map<String, Object> extra)
    {
        this.extra = extra;
    }
}
