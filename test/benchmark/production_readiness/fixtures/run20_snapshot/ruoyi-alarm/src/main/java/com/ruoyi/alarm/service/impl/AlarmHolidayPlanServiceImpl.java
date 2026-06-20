package com.ruoyi.alarm.service.impl;

import java.util.Date;
import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import com.ruoyi.alarm.domain.AlarmHolidayPlan;
import com.ruoyi.alarm.mapper.AlarmHolidayPlanMapper;
import com.ruoyi.alarm.service.IAlarmHolidayPlanService;

/**
 * 节假日计划Service业务层处理
 *
 * @author ruoyi
 */
@Service
public class AlarmHolidayPlanServiceImpl implements IAlarmHolidayPlanService
{
    @Autowired
    private AlarmHolidayPlanMapper holidayPlanMapper;

    /**
     * 查询节假日计划列表
     *
     * @param plan 节假日计划对象
     * @return 节假日计划集合
     */
    @Override
    public List<AlarmHolidayPlan> selectList(AlarmHolidayPlan plan)
    {
        return holidayPlanMapper.selectHolidayPlanList(plan);
    }

    /**
     * 查询节假日计划
     *
     * @param planId 节假日计划ID
     * @return 节假日计划
     */
    @Override
    public AlarmHolidayPlan selectById(Long planId)
    {
        return holidayPlanMapper.selectHolidayPlanById(planId);
    }

    /**
     * 新增节假日计划
     *
     * @param plan 节假日计划对象
     * @return 结果
     */
    @Override
    public int insert(AlarmHolidayPlan plan)
    {
        return holidayPlanMapper.insertHolidayPlan(plan);
    }

    /**
     * 修改节假日计划
     *
     * @param plan 节假日计划对象
     * @return 结果
     */
    @Override
    public int update(AlarmHolidayPlan plan)
    {
        return holidayPlanMapper.updateHolidayPlan(plan);
    }

    /**
     * 批量删除节假日计划
     *
     * @param planIds 需要删除的节假日计划ID
     * @return 结果
     */
    @Override
    @Transactional
    public int deleteByIds(Long[] planIds)
    {
        return holidayPlanMapper.deleteHolidayPlanByIds(planIds);
    }

    /**
     * 批量导入节假日计划
     *
     * @param plans 节假日计划集合
     * @return 结果
     */
    @Override
    @Transactional
    public int batchImport(List<AlarmHolidayPlan> plans)
    {
        int count = 0;
        for (AlarmHolidayPlan plan : plans)
        {
            // 校验同策略下日期是否重叠
            if (plan.getStrategyId() != null && plan.getStartDate() != null && plan.getEndDate() != null)
            {
                List<AlarmHolidayPlan> existing = holidayPlanMapper.selectHolidayPlanByDateRange(plan.getStartDate(), plan.getEndDate());
                boolean overlap = false;
                for (AlarmHolidayPlan exist : existing)
                {
                    if (exist.getStrategyId().equals(plan.getStrategyId()))
                    {
                        overlap = true;
                        break;
                    }
                }
                if (overlap)
                {
                    continue;
                }
            }
            count += holidayPlanMapper.insertHolidayPlan(plan);
        }
        return count;
    }

    /**
     * 按日期范围查询节假日计划
     *
     * @param startDate 开始日期
     * @param endDate 结束日期
     * @return 节假日计划集合
     */
    @Override
    public List<AlarmHolidayPlan> selectByDateRange(Date startDate, Date endDate)
    {
        return holidayPlanMapper.selectHolidayPlanByDateRange(startDate, endDate);
    }

    /**
     * 判断指定日期是否为节假日
     *
     * @param date 日期
     * @param strategyId 排班策略ID
     * @return 是否为节假日
     */
    @Override
    public boolean isHoliday(Date date, Long strategyId)
    {
        AlarmHolidayPlan plan = holidayPlanMapper.selectHolidayByDate(date, strategyId);
        return plan != null;
    }
}
