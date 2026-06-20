package com.ruoyi.alarm.domain;

import java.io.Serializable;
import com.ruoyi.common.core.domain.BaseEntity;

/**
 * 预警模板实体
 * 
 * @author ruoyi
 */
public class AlarmTemplate extends BaseEntity implements Serializable
{
    private static final long serialVersionUID = 1L;

    /** 模板ID */
    private Long templateId;

    /** 模板类型(slack/wechat_work/lark/voice) */
    private String templateType;

    /** 模板名称 */
    private String templateName;

    /** 模板内容(支持#{变量名}占位符) */
    private String templateContent;

    /** 状态(0=启用,1=禁用) */
    private String status;

    public Long getTemplateId()
    {
        return templateId;
    }

    public void setTemplateId(Long templateId)
    {
        this.templateId = templateId;
    }

    public String getTemplateType()
    {
        return templateType;
    }

    public void setTemplateType(String templateType)
    {
        this.templateType = templateType;
    }

    public String getTemplateName()
    {
        return templateName;
    }

    public void setTemplateName(String templateName)
    {
        this.templateName = templateName;
    }

    public String getTemplateContent()
    {
        return templateContent;
    }

    public void setTemplateContent(String templateContent)
    {
        this.templateContent = templateContent;
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
