package com.ruoyi.alarm.domain.dto;

import java.util.List;
import java.util.Map;

/**
 * 对外预警API请求DTO集合
 * 
 * @author ruoyi
 */
public class NotifyRequestDTO
{
    /**
     * 简单预警请求
     */
    public static class NotifySimpleRequest
    {
        /** 应用ID */
        private Long appId;

        /** 应用密钥 */
        private String appSecret;

        /** 任务名称 */
        private String taskName;

        /** 幂等值 */
        private String idempotentValue;

        /** 模板变量 */
        private Map<String, String> variables;

        /** 通知用户ID列表（逗号分隔） */
        private String notifyUserIds;

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

        public String getTaskName()
        {
            return taskName;
        }

        public void setTaskName(String taskName)
        {
            this.taskName = taskName;
        }

        public String getIdempotentValue()
        {
            return idempotentValue;
        }

        public void setIdempotentValue(String idempotentValue)
        {
            this.idempotentValue = idempotentValue;
        }

        public Map<String, String> getVariables()
        {
            return variables;
        }

        public void setVariables(Map<String, String> variables)
        {
            this.variables = variables;
        }

        public String getNotifyUserIds()
        {
            return notifyUserIds;
        }

        public void setNotifyUserIds(String notifyUserIds)
        {
            this.notifyUserIds = notifyUserIds;
        }
    }

    /**
     * 组合预警请求
     */
    public static class NotifyComposeRequest
    {
        /** 应用ID */
        private Long appId;

        /** 应用密钥 */
        private String appSecret;

        /** 任务名称 */
        private String taskName;

        /** 幂等值 */
        private String idempotentValue;

        /** 标题 */
        private String title;

        /** 内容 */
        private String content;

        /** 渠道配置列表 */
        private List<NotifyChannelConfig> channels;

        /** 通知用户ID列表（逗号分隔） */
        private String notifyUserIds;

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

        public String getTaskName()
        {
            return taskName;
        }

        public void setTaskName(String taskName)
        {
            this.taskName = taskName;
        }

        public String getIdempotentValue()
        {
            return idempotentValue;
        }

        public void setIdempotentValue(String idempotentValue)
        {
            this.idempotentValue = idempotentValue;
        }

        public String getTitle()
        {
            return title;
        }

        public void setTitle(String title)
        {
            this.title = title;
        }

        public String getContent()
        {
            return content;
        }

        public void setContent(String content)
        {
            this.content = content;
        }

        public List<NotifyChannelConfig> getChannels()
        {
            return channels;
        }

        public void setChannels(List<NotifyChannelConfig> channels)
        {
            this.channels = channels;
        }

        public String getNotifyUserIds()
        {
            return notifyUserIds;
        }

        public void setNotifyUserIds(String notifyUserIds)
        {
            this.notifyUserIds = notifyUserIds;
        }
    }

    /**
     * 渠道配置
     */
    public static class NotifyChannelConfig
    {
        /** 渠道类型 */
        private String channelType;

        /** 机器人ID */
        private Long robotId;

        /** 模板ID */
        private Long templateId;

        /** 回调启用 */
        private Boolean callbackEnabled;

        /** 分隔符启用 */
        private Boolean dividerEnabled;

        /** 电话号码 */
        private String phoneNumber;

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

        public Long getTemplateId()
        {
            return templateId;
        }

        public void setTemplateId(Long templateId)
        {
            this.templateId = templateId;
        }

        public Boolean getCallbackEnabled()
        {
            return callbackEnabled;
        }

        public void setCallbackEnabled(Boolean callbackEnabled)
        {
            this.callbackEnabled = callbackEnabled;
        }

        public Boolean getDividerEnabled()
        {
            return dividerEnabled;
        }

        public void setDividerEnabled(Boolean dividerEnabled)
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
    }

    /**
     * 恢复请求
     */
    public static class NotifyRecoverRequest
    {
        /** 应用ID */
        private Long appId;

        /** 应用密钥 */
        private String appSecret;

        /** 任务名称 */
        private String taskName;

        /** 幂等值 */
        private String idempotentValue;

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

        public String getTaskName()
        {
            return taskName;
        }

        public void setTaskName(String taskName)
        {
            this.taskName = taskName;
        }

        public String getIdempotentValue()
        {
            return idempotentValue;
        }

        public void setIdempotentValue(String idempotentValue)
        {
            this.idempotentValue = idempotentValue;
        }
    }

    /**
     * 删除请求
     */
    public static class NotifyDeleteRequest
    {
        /** 应用ID */
        private Long appId;

        /** 应用密钥 */
        private String appSecret;

        /** 任务名称 */
        private String taskName;

        /** 幂等值 */
        private String idempotentValue;

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

        public String getTaskName()
        {
            return taskName;
        }

        public void setTaskName(String taskName)
        {
            this.taskName = taskName;
        }

        public String getIdempotentValue()
        {
            return idempotentValue;
        }

        public void setIdempotentValue(String idempotentValue)
        {
            this.idempotentValue = idempotentValue;
        }
    }

    /**
     * VoIP发送请求
     */
    public static class VoipSendRequest
    {
        /** 应用ID */
        private Long appId;

        /** 应用密钥 */
        private String appSecret;

        /** 任务名称 */
        private String taskName;

        /** 幂等值 */
        private String idempotentValue;

        /** VoIP ID */
        private String voipId;

        /** 内容 */
        private String content;

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

        public String getTaskName()
        {
            return taskName;
        }

        public void setTaskName(String taskName)
        {
            this.taskName = taskName;
        }

        public String getIdempotentValue()
        {
            return idempotentValue;
        }

        public void setIdempotentValue(String idempotentValue)
        {
            this.idempotentValue = idempotentValue;
        }

        public String getVoipId()
        {
            return voipId;
        }

        public void setVoipId(String voipId)
        {
            this.voipId = voipId;
        }

        public String getContent()
        {
            return content;
        }

        public void setContent(String content)
        {
            this.content = content;
        }
    }

    /**
     * 回调请求
     */
    public static class CallbackRequest
    {
        /** 记录ID */
        private Long recordId;

        /** 回调类型 */
        private Integer callbackType;

        public Long getRecordId()
        {
            return recordId;
        }

        public void setRecordId(Long recordId)
        {
            this.recordId = recordId;
        }

        public Integer getCallbackType()
        {
            return callbackType;
        }

        public void setCallbackType(Integer callbackType)
        {
            this.callbackType = callbackType;
        }
    }

    /**
     * 响应结果
     */
    public static class NotifyResponse
    {
        /** 响应码 */
        private Integer code;

        /** 响应消息 */
        private String msg;

        /** 数据 */
        private Long data;

        public NotifyResponse()
        {
        }

        public NotifyResponse(Integer code, String msg)
        {
            this.code = code;
            this.msg = msg;
        }

        public NotifyResponse(Integer code, String msg, Long data)
        {
            this.code = code;
            this.msg = msg;
            this.data = data;
        }

        public Integer getCode()
        {
            return code;
        }

        public void setCode(Integer code)
        {
            this.code = code;
        }

        public String getMsg()
        {
            return msg;
        }

        public void setMsg(String msg)
        {
            this.msg = msg;
        }

        public Long getData()
        {
            return data;
        }

        public void setData(Long data)
        {
            this.data = data;
        }

        /**
         * 成功响应
         * 
         * @return 成功响应
         */
        public static NotifyResponse success()
        {
            return new NotifyResponse(0, "success");
        }

        /**
         * 成功响应
         * 
         * @param data 数据
         * @return 成功响应
         */
        public static NotifyResponse success(Long data)
        {
            return new NotifyResponse(0, "success", data);
        }

        /**
         * 错误响应
         * 
         * @param code 错误码
         * @param msg 错误消息
         * @return 错误响应
         */
        public static NotifyResponse error(Integer code, String msg)
        {
            return new NotifyResponse(code, msg);
        }
    }
}
