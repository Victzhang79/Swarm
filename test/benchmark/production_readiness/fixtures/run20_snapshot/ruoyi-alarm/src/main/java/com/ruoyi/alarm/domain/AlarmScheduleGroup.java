package com.ruoyi.alarm.domain;

import java.io.Serializable;
import com.ruoyi.common.core.domain.BaseEntity;

/**
 * 排班分组 alarm_schedule_group
 *
 * @author ruoyi
 */
public class AlarmScheduleGroup extends BaseEntity implements Serializable
{
    private static final long serialVersionUID = 1L;

    /** 分组ID */
    private Long groupId;

    /** 策略ID */
    private Long strategyId;

    /** 分组序号 */
    private Integer groupIndex;

    /** 分组名称 */
    private String groupName;

    /** 成员ID列表（逗号分隔） */
    private String memberIds;

    public Long getGroupId()
    {
        return groupId;
    }

    public void setGroupId(Long groupId)
    {
        this.groupId = groupId;
    }

    public Long getStrategyId()
    {
        return strategyId;
    }

    public void setStrategyId(Long strategyId)
    {
        this.strategyId = strategyId;
    }

    public Integer getGroupIndex()
    {
        return groupIndex;
    }

    public void setGroupIndex(Integer groupIndex)
    {
        this.groupIndex = groupIndex;
    }

    public String getGroupName()
    {
        return groupName;
    }

    public void setGroupName(String groupName)
    {
        this.groupName = groupName;
    }

    public String getMemberIds()
    {
        return memberIds;
    }

    public void setMemberIds(String memberIds)
    {
        this.memberIds = memberIds;
    }
}
