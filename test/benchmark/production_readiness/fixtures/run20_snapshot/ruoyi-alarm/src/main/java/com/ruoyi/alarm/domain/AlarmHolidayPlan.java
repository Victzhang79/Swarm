package com.ruoyi.alarm.domain;

import java.util.Date;
import java.io.Serializable;
import com.fasterxml.jackson.annotation.JsonFormat;
import com.fasterxml.jackson.annotation.JsonFormat.Shape;
import org.apache.commons.lang3.builder.ToStringBuilder;
import org.apache.commons.lang3.builder.ToStringStyle;
import com.ruoyi.common.core.domain.BaseEntity;

/**
 * 节假日计划 alarm_holiday_plan
 *
 * @author ruoyi
 */
public class AlarmHolidayPlan extends BaseEntity implements Serializable
{
    private static final long serialVersionUID = 1L;

    /** 计划ID */
    private Long planId;

    /** 排班策略ID */
    private Long strategyId;

    /** 节假日名称 */
    private String holidayName;

    /** 开始日期 */
    @JsonFormat(pattern = "yyyy-MM-dd", timezone = "GMT+8", shape = Shape.STRING)
    private Date startDate;

    /** 结束日期 */
    @JsonFormat(pattern = "yyyy-MM-dd", timezone = "GMT+8", shape = Shape.STRING)
    private Date endDate;

    /** 成员ID列表（逗号分隔） */
    private String memberIds;

    public Long getPlanId()
    {
        return planId;
    }

    public void setPlanId(Long planId)
    {
        this.planId = planId;
    }

    public Long getStrategyId()
    {
        return strategyId;
    }

    public void setStrategyId(Long strategyId)
    {
        this.strategyId = strategyId;
    }

    public String getHolidayName()
    {
        return holidayName;
    }

    public void setHolidayName(String holidayName)
    {
        this.holidayName = holidayName;
    }

    public Date getStartDate()
    {
        return startDate;
    }

    public void setStartDate(Date startDate)
    {
        this.startDate = startDate;
    }

    public Date getEndDate()
    {
        return endDate;
    }

    public void setEndDate(Date endDate)
    {
        this.endDate = endDate;
    }

    public String getMemberIds()
    {
        return memberIds;
    }

    public void setMemberIds(String memberIds)
    {
        this.memberIds = memberIds;
    }

    @Override
    public String toString()
    {
        return new ToStringBuilder(this, ToStringStyle.MULTI_LINE_STYLE)
            .append("planId", getPlanId())
            .append("strategyId", getStrategyId())
            .append("holidayName", getHolidayName())
            .append("startDate", getStartDate())
            .append("endDate", getEndDate())
            .append("memberIds", getMemberIds())
            .append("createBy", getCreateBy())
            .append("createTime", getCreateTime())
            .append("updateBy", getUpdateBy())
            .append("updateTime", getUpdateTime())
            .append("remark", getRemark())
            .toString();
    }
}
