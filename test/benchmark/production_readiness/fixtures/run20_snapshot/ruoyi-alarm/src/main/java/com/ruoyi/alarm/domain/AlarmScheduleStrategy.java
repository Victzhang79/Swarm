package com.ruoyi.alarm.domain;

import java.io.Serializable;
import java.util.List;
import com.fasterxml.jackson.annotation.JsonInclude;
import com.ruoyi.common.core.domain.BaseEntity;

/**
 * 排班策略 alarm_schedule_strategy
 *
 * @author ruoyi
 */
public class AlarmScheduleStrategy extends BaseEntity implements Serializable
{
    private static final long serialVersionUID = 1L;

    /** 策略ID */
    private Long strategyId;

    /** 策略名称 */
    private String strategyName;

    /** 排班模式：rotation=轮值，holiday_priority=节假日优先 */
    private String strategyMode;

    /** 分组数量 */
    private Integer groupCount;

    /** 状态：0=启用，1=禁用 */
    private String status;

    @JsonInclude(JsonInclude.Include.NON_EMPTY)
    private List<AlarmScheduleGroup> groups;

    public Long getStrategyId()
    {
        return strategyId;
    }

    public void setStrategyId(Long strategyId)
    {
        this.strategyId = strategyId;
    }

    public String getStrategyName()
    {
        return strategyName;
    }

    public void setStrategyName(String strategyName)
    {
        this.strategyName = strategyName;
    }

    public String getStrategyMode()
    {
        return strategyMode;
    }

    public void setStrategyMode(String strategyMode)
    {
        this.strategyMode = strategyMode;
    }

    public Integer getGroupCount()
    {
        return groupCount;
    }

    public void setGroupCount(Integer groupCount)
    {
        this.groupCount = groupCount;
    }

    public String getStatus()
    {
        return status;
    }

    public void setStatus(String status)
    {
        this.status = status;
    }

    public List<AlarmScheduleGroup> getGroups()
    {
        return groups;
    }

    public void setGroups(List<AlarmScheduleGroup> groups)
    {
        this.groups = groups;
    }
}
