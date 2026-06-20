package com.ruoyi.alarm.service.impl;

import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import com.ruoyi.alarm.domain.AlarmScheduleGroup;
import com.ruoyi.alarm.domain.AlarmScheduleStrategy;
import com.ruoyi.alarm.mapper.AlarmScheduleGroupMapper;
import com.ruoyi.alarm.mapper.AlarmScheduleStrategyMapper;
import com.ruoyi.alarm.service.IAlarmScheduleStrategyService;

/**
 * 排班策略 Service业务层处理
 * 
 * @author ruoyi
 */
@Service
public class AlarmScheduleStrategyServiceImpl implements IAlarmScheduleStrategyService
{
    @Autowired
    private AlarmScheduleStrategyMapper scheduleStrategyMapper;

    @Autowired
    private AlarmScheduleGroupMapper scheduleGroupMapper;

    /**
     * 查询排班策略列表
     * 
     * @param strategy 排班策略
     * @return 排班策略
     */
    @Override
    public List<AlarmScheduleStrategy> selectScheduleStrategyList(AlarmScheduleStrategy strategy)
    {
        return scheduleStrategyMapper.selectScheduleStrategyList(strategy);
    }

    /**
     * 查询排班策略
     * 
     * @param strategyId 排班策略主键
     * @return 排班策略
     */
    @Override
    public AlarmScheduleStrategy selectScheduleStrategyById(Long strategyId)
    {
        return scheduleStrategyMapper.selectScheduleStrategyById(strategyId);
    }

    /**
     * 新增排班策略和排班分组
     * 
     * @param strategy 排班策略
     * @param groups 排班分组集合
     * @return 结果
     */
    @Override
    @Transactional
    public int insertScheduleStrategyWithGroups(AlarmScheduleStrategy strategy, List<AlarmScheduleGroup> groups)
    {
        scheduleStrategyMapper.insertScheduleStrategy(strategy);
        if (groups != null && !groups.isEmpty())
        {
            for (AlarmScheduleGroup group : groups)
            {
                group.setStrategyId(strategy.getStrategyId());
            }
            scheduleGroupMapper.batchInsertScheduleGroup(groups);
        }
        return 1;
    }

    /**
     * 修改排班策略和排班分组
     * 
     * @param strategy 排班策略
     * @param groups 排班分组集合
     * @return 结果
     */
    @Override
    @Transactional
    public int updateScheduleStrategyWithGroups(AlarmScheduleStrategy strategy, List<AlarmScheduleGroup> groups)
    {
        scheduleStrategyMapper.updateScheduleStrategy(strategy);
        scheduleGroupMapper.deleteScheduleGroupByStrategyId(strategy.getStrategyId());
        if (groups != null && !groups.isEmpty())
        {
            for (AlarmScheduleGroup group : groups)
            {
                group.setStrategyId(strategy.getStrategyId());
            }
            scheduleGroupMapper.batchInsertScheduleGroup(groups);
        }
        return 1;
    }

    /**
     * 批量删除排班策略
     * 
     * @param strategyIds 需要删除的排班策略主键
     * @return 结果
     */
    @Override
    @Transactional
    public int deleteScheduleStrategyByIds(Long[] strategyIds)
    {
        scheduleGroupMapper.deleteScheduleGroupByStrategyIds(strategyIds);
        return scheduleStrategyMapper.deleteScheduleStrategyByIds(strategyIds);
    }

    /**
     * 修改排班策略状态
     * 
     * @param strategy 排班策略
     * @return 结果
     */
    @Override
    public int changeStatus(AlarmScheduleStrategy strategy)
    {
        return scheduleStrategyMapper.updateScheduleStrategy(strategy);
    }
}
