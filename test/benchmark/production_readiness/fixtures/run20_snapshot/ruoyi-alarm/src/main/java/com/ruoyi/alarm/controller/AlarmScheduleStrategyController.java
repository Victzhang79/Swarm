package com.ruoyi.alarm.controller;

import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseBody;
import org.springframework.web.bind.annotation.RestController;
import com.ruoyi.alarm.domain.AlarmScheduleGroup;
import com.ruoyi.alarm.domain.AlarmScheduleStrategy;
import com.ruoyi.alarm.service.IAlarmScheduleStrategyService;
import com.ruoyi.common.annotation.Log;
import com.ruoyi.common.core.controller.BaseController;
import com.ruoyi.common.core.domain.AjaxResult;
import com.ruoyi.common.core.page.TableDataInfo;
import com.ruoyi.common.enums.BusinessType;

/**
 * 排班策略Controller
 * 
 * @author ruoyi
 */
@RestController
@RequestMapping("/alarm/schedule/strategy")
public class AlarmScheduleStrategyController extends BaseController
{
    private String prefix = "alarm/schedule/strategy";

    @Autowired
    private IAlarmScheduleStrategyService scheduleStrategyService;

    /**
     * 查询排班策略列表
     */
    @RequiresPermissions("alarm:schedule:strategy:list")
    @GetMapping("/list")
    public TableDataInfo list(AlarmScheduleStrategy strategy)
    {
        startPage();
        List<AlarmScheduleStrategy> list = scheduleStrategyService.selectScheduleStrategyList(strategy);
        return getDataTable(list);
    }

    /**
     * 获取排班策略详情
     */
    @RequiresPermissions("alarm:schedule:strategy:query")
    @GetMapping("/detail/{strategyId}")
    public AjaxResult detail(@PathVariable("strategyId") Long strategyId)
    {
        AlarmScheduleStrategy strategy = scheduleStrategyService.selectScheduleStrategyById(strategyId);
        return AjaxResult.success(strategy);
    }

    /**
     * 新增排班策略
     */
    @RequiresPermissions("alarm:schedule:strategy:add")
    @Log(title = "排班策略", businessType = BusinessType.INSERT)
    @PostMapping("/add")
    public AjaxResult add(AlarmScheduleStrategy strategy)
    {
        int result = scheduleStrategyService.insertScheduleStrategyWithGroups(strategy, strategy.getGroups());
        return toAjax(result);
    }

    /**
     * 修改排班策略
     */
    @RequiresPermissions("alarm:schedule:strategy:edit")
    @Log(title = "排班策略", businessType = BusinessType.UPDATE)
    @PostMapping("/edit")
    public AjaxResult edit(AlarmScheduleStrategy strategy)
    {
        int result = scheduleStrategyService.updateScheduleStrategyWithGroups(strategy, strategy.getGroups());
        return toAjax(result);
    }

    /**
     * 删除排班策略
     */
    @RequiresPermissions("alarm:schedule:strategy:remove")
    @Log(title = "排班策略", businessType = BusinessType.DELETE)
    @PostMapping("/remove")
    public AjaxResult remove(@RequestParam("strategyIds") Long[] strategyIds)
    {
        int result = scheduleStrategyService.deleteScheduleStrategyByIds(strategyIds);
        return toAjax(result);
    }

    /**
     * 修改排班策略状态
     */
    @RequiresPermissions("alarm:schedule:strategy:edit")
    @Log(title = "排班策略", businessType = BusinessType.UPDATE)
    @PostMapping("/changeStatus")
    public AjaxResult changeStatus(AlarmScheduleStrategy strategy)
    {
        int result = scheduleStrategyService.changeStatus(strategy);
        return toAjax(result);
    }
}
